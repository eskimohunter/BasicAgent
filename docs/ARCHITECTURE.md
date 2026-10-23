# Architecture

This document describes the internal design of BA: its components, the
data flow of a turn, and the reasoning behind the main decisions. It is intended
for anyone auditing or extending `agent.py`.

The implementation code lives in one file, `agent.py` (2,902 physical lines, of
which 2,122 are executable). The file is divided by banners: Constants, Tools,
Approvals, LLM client, Agent loop, and Application. Line numbers below are inline
references to the current revision; function names are the stable anchors.

## 1. Goals and non-goals

**Goals**

1. **Understandable** — a linear, synchronous control flow that can be read
   top-to-bottom in one sitting. No frameworks, no plugin system, no hidden state.
2. **Auditable** — messages, tool calls, approval decisions and results are
   appended to a JSONL log when logging is available. Shell commands are
   displayed exactly as executed.
3. **Safe by construction** — PLAN mode physically cannot modify the host, and
   BUILD mode cannot run a shell command without explicit user consent.
4. **Cross-platform** — one file runs on Linux and Windows; `prompt_toolkit`
   is the only required third-party dependency (`pygments` is optional).

**Non-goals (for now)**

- Multi-agent orchestration, MCP, embeddings, or RAG.
- Session persistence/resume and context compaction.
- Sandboxing the shell beyond user approval and a working directory.
- General retry/backoff logic beyond the explicit one-shot fallbacks described
  below, runtime model fallback, or provider features beyond the existing Open
  WebUI backend.

## 2. Component map

| Component | Location | Responsibility |
| --- | --- | --- |
| Constants | `agent.py:58-293` | Identity, limits, timeouts, UI text, wire-format values, markdown patterns and the style sheet |
| `AgentError` / `ToolError` | `agent.py:296-297`, `agent.py:308-309` | User-facing failure vs. model-visible tool failure |
| `Mode` | `agent.py:312-316` | `PLAN` / `BUILD` enum |
| `Config` | `agent.py:320-341` | All runtime settings |
| `parse_args` | `agent.py:378-474` | CLI parsing, env fallbacks, precedence |
| `load_dotenv` | `agent.py:344-375` | `KEY=VALUE` env files (cwd then app dir), never overriding |
| `probe_context_window` | `agent.py:477-525` | Startup context-window probe (`/props`, `/models`) |
| `AuditLog` | `agent.py:528-565` | Append-only JSONL writer |
| `say` / `stream_write` / `Spinner` | `agent.py:568-617` | Terminal output helpers and wait spinner |
| `KeyWatcher` | `agent.py:620-688` | Raw stdin watcher; Esc during streaming raises SIGINT |
| `MarkdownStream` | `agent.py:743-1135` | Incremental markdown rendering |
| `cap` | `agent.py:1138-1142` | Tool/result truncation |
| `Tool` | `agent.py:1151-1163` | Tool metadata: schema, handler, flags |
| `resolve_path` / `is_excluded` | `agent.py:1183-1221` | Workspace boundary and directory excludes |
| `tool_*` handlers | `agent.py:1258-1545` | The seven tools |
| `build_tools` | `agent.py:1548-1648` | Registry: name → `Tool` |
| `find_agents_file` / `load_project_instructions` | `agent.py:1651-1678` | Workspace `Agents.md` → system prompt |
| `build_system_prompt` | `agent.py:1681-1721` | Mode-aware system prompt (incl. project instructions) |
| `Approvals` | `agent.py:1729-1815` | Yes/No button approval gate + allowlist seam |
| `StreamResult` | `agent.py:1824-1833` | One assistant reply: message dict + interrupted flag |
| `parse_non_stream_response` | `agent.py:1836-1876` | Fallback for servers that ignore `stream: true` |
| `finalize_tool_calls` | `agent.py:1879-1898` | Merge streamed tool-call fragments |
| `chat_completions_url` / `auth_headers` | `agent.py:1905-1909`, `agent.py:1959-1972` | API-style-aware endpoint and `Authorization` header |
| `openwebui_signin` / `openwebui_token` | `agent.py:1919-1939`, `agent.py:1942-1956` | JWT sign-in and per-endpoint token cache |
| `stream_chat` | `agent.py:2034-2199` | HTTP POST + SSE parsing + live rendering |
| `summarise_args` | `agent.py:2207-2220` | One-line tool-call display |
| `summarise_command` / `describe_command` | `agent.py:2223-2310` | One-line command explanation via the summary model, cached per session |
| `execute_tool` | `agent.py:2313-2383` | Dispatch pipeline: validate → guard → approve → run → log |
| `run_turn` | `agent.py:2386-2489` | One user turn: stream → tools → repeat |
| `load_init_prompt` / `reload_project_instructions` | `agent.py:2515-2529`, `agent.py:2532-2544` | `/init` prompt loading and system-prompt refresh |
| `handle_command` | `agent.py:2547-2604` | Slash commands |
| `WorkspacePathCompleter` | `agent.py:2607-2651` | Workspace-relative path completion for the prompt |
| `App` | `agent.py:2659-2716` | Wires config, log, session, mode, messages, tools, approvals |
| `estimate_tokens` / `context_tokens` | `agent.py:2719-2748` | Context usage estimate + exact/estimate reconciliation |
| `build_toolbar` / `build_key_bindings` | `agent.py:2751-2790` | Status bar (mode, model, ctx) and Tab binding |
| `print_startup_instructions` | `agent.py:2812-2828` | Renders `Instructions.md` next to `agent.py` after the banner |
| `main` | `agent.py:2831-2898` | Banner, REPL loop, shutdown |
| `config/prompts/init.md` | app resource | Prompt text used by `/init` |

## 3. State model

`App` (`agent.py:2659`) holds the main mutable session state:

```python
class App:
    config: Config
    log: AuditLog
    session: PromptSession
    mode: Mode                      # starts as PLAN
    messages: list[dict]            # OpenAI chat messages; messages[0] is system
    tools: dict[str, Tool]
    approvals: Approvals            # Yes/No gate + allowlist seam
    stream_usage: bool
    exact_context: int
    exact_context_at: int
    summaries: dict[str, str]
    summary_disabled: bool
```

`messages` is the single source of conversation truth and is sent verbatim to the
server each request. The system prompt at `messages[0]` is rebuilt whenever the
mode changes (`App.toggle_mode`, `agent.py:2702`), so mode instructions always
reflect the current mode. `/clear` resets `messages` to just the system prompt.
The prompt also embeds workspace project instructions (`Agents.md`, loaded at
startup and reloaded after `/init`; see §4).

Endpoint and workspace configuration is immutable after startup, though `/init`
may reload the project instructions. Precedence is CLI > environment >
`.env` in the current directory > `.env` next to `agent.py` > default, resolved
entirely in `parse_args` (`agent.py:378`). `load_dotenv` (`agent.py:344`) fills
`os.environ` from those files without overriding existing variables, so
environment variables remain the intended way to set machine-specific values
such as `AGENT_BASE_URL`.

## 4. Startup

`main` (`agent.py:2831`):

1. Parse configuration, then clear the terminal.
2. Load workspace project instructions (`Agents.md`,
   any capitalization, capped at 32,000 characters) into the system prompt.
3. Unless a positive `AGENT_CONTEXT_WINDOW` is set, probe the server for the
   context window (`/props`, then `/models`); each request gets a 5 s timeout
   and the probe falls back to unknown.
4. Attempt to open the audit log (best-effort; there is no disable switch) and
   write `session_start` with a redacted API key and the resolved context window.
5. Create the `PromptSession` with the shared style sheet and the
   `WorkspacePathCompleter`, then create the `App` and key bindings.
6. Print the ASCII-art `Basic Agent` banner with `Version:`, then the endpoint,
   model (with the summary model in brackets when one is set) and workspace.
7. Render `Instructions.md` next to `agent.py` as markdown, if present.
8. Enter the REPL loop.

The session carries the `WorkspacePathCompleter`; each `session.prompt(...)` call
supplies the Tab, Esc and Ctrl+C key bindings and a `bottom_toolbar` callable.
The completer handles path-like words (containing `/` or `\`) from the workspace,
hiding `DEFAULT_EXCLUDES` directories. The Tab binding is filtered with
`~has_completions`, so it cycles completions while the menu is open and toggles
the mode otherwise. `Esc` resets the input buffer; `Ctrl+C` exits with `QuitApp`
and `Ctrl+D` raises `EOFError`. Input starting with `/` goes to
`handle_command` (which may itself run a turn for `/init`); everything else goes
to `run_turn`. The loop ends via `/exit`, `/quit`, `Ctrl+C`, `Ctrl+D`, or an
uncaught exception, and `session_end` is always written in a `finally` block.

## 5. Turn lifecycle

`run_turn` (`agent.py:2386`) implements the agent loop. One user turn:

```
user text
   │
   ├─ append {"role":"user"} ──────────────────────────────► audit: user_message
   │
   └─ repeat up to MAX_STEPS times:
        │
        ├─ stream_chat(app, on_delta) ──────► POST chat_completions_url(...)
        │      │                                stream: true, tools: allowed schemas
        │      │                                (spinner runs until first output)
        │      │
        │      ├─ SSE content deltas ──────► MarkdownStream.feed → terminal
        │      └─ SSE tool_call deltas ────► accumulated by index
        │
        ├─ append assistant message ───────────────────────► audit: assistant_message
        │
        ├─ no tool calls? ──► turn ends
        │
        └─ for each tool call:
             ├─ execute_tool:
             │    ├─ parse/validate arguments
             │    ├─ unknown tool?            → ERROR result
             │    ├─ write tool in PLAN?      → ERROR result
             │    ├─ needs approval? → Approvals.ask ──► audit: approval
             │    │      └─ denied           → ERROR result
             │    ├─ log tool_call             ─────────────────► audit: tool_call
             │    ├─ run handler
             │    └─ cap + log result         ─────────────────► audit: tool_result
             └─ append {"role":"tool", "tool_call_id", "content"}
```

If the loop exhausts `MAX_STEPS`, a warning is printed and logged, and the turn
ends. This bounds runaway tool loops.

### Wait indicator

`stream_chat` is wrapped in a `Spinner` (`agent.py:578`): a daemon thread that
animates an ASCII frame on the current line until the renderer produces its
first output, or until the stream ends if the model only emits tool calls. When
tool calls arrive, the spinner stays active (or is restarted after streamed
content is finalized) while command summaries are prefetched
(`describe_command`), so the user sees a single wait; it is stopped before
anything else is printed. The thread writes directly to `stdout` and a short
grace period suppresses the spinner for fast responses.

### Reply rendering

Content deltas are fed to `MarkdownStream` (`agent.py:743`), which emits
`FormattedText` fragments through the same `print_formatted_text` path as `say`.
It is an incremental state machine: block prefixes (headings, lists, tasks,
quotes, rules, fences, tables) are recognised at line starts, while inline spans
and partial markers are held only until they resolve, so paragraphs keep
streaming. Fenced code is highlighted per line through Pygments when available
(`pygments` is a soft dependency). `finalize` flushes unresolved spans and, when
any text was written, adds a trailing newline. `http(s)` links are emitted as
`[ZeroWidthEscape]` fragments wrapping the text in OSC 8 sequences when the
active output is a VT terminal (`osc8_supported`), so prompt_toolkit passes them
through raw without counting them in the layout. The grey ` (url)` suffix is
always printed after the link text, whether or not OSC 8 is active.

### Interruption

`Esc` while streaming is picked up by `KeyWatcher` (a daemon thread reading raw
keys), which raises `SIGINT`; the resulting `KeyboardInterrupt` inside
`stream_chat` becomes an interrupted `StreamResult`. The partial text is kept,
`[interrupted by user]` is appended, and `run_turn` returns without executing
any partially-received tool calls (their argument JSON would be incomplete and
possibly dangerous to guess at). A real `Ctrl+C` raises the same exception with
`escaped` unset, so `stream_chat` re-raises and BA quits.

## 6. LLM client

`stream_chat` (`agent.py:2034`) uses only `urllib.request` and `json` — no HTTP
library.

**Request**

```json
{
  "model": "<config.model>",
  "messages": [ ... ],
  "stream": true,
  "stream_options": { "include_usage": true },  // dropped after HTTP 400
  "tools": [ ... ],        // only when the mode permits at least one tool
  "tool_choice": "auto"    // included with tools
}
```

`Authorization: Bearer <api_key>` is always sent; LAN servers that ignore auth
accept the default `none`.

**Usage.** `stream_options.include_usage` asks the server for a final `usage`
chunk. If the request is rejected with HTTP 400, `stream_chat` retries once
without it and disables it for the rest of the session. Usage is also read from
non-stream bodies. `App.record_usage` stores `prompt_tokens + completion_tokens`
along with the message count; the toolbar uses that exact value until more
messages are appended, then falls back to the byte estimate (`estimate_tokens`).

**SSE parsing.** The response is consumed line by line. Only `data:` lines are
considered; `[DONE]` terminates the stream; unparseable chunks are skipped
defensively. For each chunk, `choices[0].delta` (or `.message`, for servers that
emit complete messages mid-stream) is inspected:

- `delta.content` is appended to the reply and passed to `on_delta` for live
  rendering.
- `delta.tool_calls[]` fragments are accumulated in `calls` keyed by `index`:
  `id` and `function.name` are captured when present (names that arrive split
  across chunks are appended unless already a suffix), and
  `function.arguments` fragments are concatenated.

`finalize_tool_calls` (`agent.py:1879`) converts the accumulator into the exact
OpenAI `tool_calls` shape, defaulting empty argument strings to `{}`. The
assistant message is then appended verbatim so the subsequent `role: "tool"`
messages reference valid `tool_call_id`s.

**Non-stream fallback.** If the response `Content-Type` is not
`text/event-stream`, the body is parsed as a normal completion by
`parse_non_stream_response` (`agent.py:1836`). This covers servers that silently
ignore `stream: true`, and surfaces `{"error": ...}` bodies as `AgentError`.

**Retry policy.** There is no general retry/backoff. `stream_chat` drops
`stream_options` once after HTTP 400; chat and command-summary requests each
retry once after an Open WebUI HTTP 401 by re-signing in. A request that still
fails raises `AgentError`, which `run_turn` prints and logs; the user's message
stays in history, so the user can simply retry.

**Open WebUI auth.** With `AGENT_API_STYLE=openwebui` (env/`.env` only), chat
requests go to `{base_url}/api/chat/completions` and `auth_headers` signs in via
`POST {base_url}/api/v1/auths/signin` with `OPENWEBUI_EMAIL`/`OPENWEBUI_PASSWORD`
(JSON, no credentials in the URL or CLI). `openwebui_token` caches the returned
JWT per endpoint and refreshes it 60 s before `expires_at` (a missing or
non-numeric expiry caches it indefinitely); a chat request answered with HTTP 401
triggers one forced re-sign-in and retry. Sign-in failures raise `AgentError`
without echoing the response body, and neither password nor token is ever
logged. The context-window probe is skipped for this style.

**Command summaries.** `summarise_command` (`agent.py:2223`) sends a
non-streaming completion to the style-appropriate chat URL (see
`chat_completions_url`) asking for one junior-developer-friendly sentence
(`temperature: 0`, 5 s timeout). The three
summary settings (`--summary-base-url`,
`--summary-api-key`, `--summary-model` or their `AGENT_SUMMARY_*`
environment variables) each fall back to the corresponding main setting, so a
second lightweight model is optional. Any failure returns `None`.

## 7. Tool system

Each tool is a `Tool` dataclass (`agent.py:1151`):

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema
    handler: Callable[..., str]
    writes: bool = False      # blocked in PLAN mode
    requires_approval: bool = False
```

`build_tools` (`agent.py:1548`) closes over `Config` with lambdas so handlers are
plain functions with keyword arguments. `tool_schema` converts a `Tool` into the
OpenAI function schema.

**Dispatch pipeline** (`execute_tool`, `agent.py:2313`), in order:

1. Parse `function.arguments` as JSON. Malformed JSON returns an `ERROR:` string
   rather than raising, so the model can correct itself.
2. Reject unknown tool names.
3. **Mode guard** — a `writes` tool in PLAN mode is refused, even if the model
   somehow requests it. This is the second enforcement layer (the first is not
   sending the schema at all).
4. **Approval gate** — `requires_approval` tools (only `run_command`) are checked
   against the allowlist; if absent, the Yes/No `Approvals.ask` prompt runs.
5. Log `tool_call` with full arguments.
6. Execute the handler. `ToolError` becomes `ERROR: <message>`; `TypeError`
   (bad argument names/types) and unexpected exceptions are converted likewise,
   so a failing tool does not abort the loop.
7. Cap the result and log `tool_result`.

Handlers always return strings. Errors are strings starting with `ERROR:`,
which the system prompt tells the model to interpret as failure.

Before dispatch, `run_turn` prepares a label per tool call. For `run_command` it
first fetches (and caches) a one-line summary with `describe_command`, then
prints `-> run_command <command>` and the summary on the following line; the
approval prompt and audit log still contain the raw command. A failed summary
falls back to the raw command and disables summarisation for the rest of the
session.

### Per-tool behaviour and limits

| Tool | Implementation notes |
| --- | --- |
| `read_file` | Binary detection (BOM-aware UTF-8/16/32; BOM-less UTF-16 guessed from NUL parity; NUL bytes without a UTF-16 pattern mean binary); decode with replacement; numbered lines; `READ_MAX_LINES` (2000) cap with an explicit header note |
| `list_dir` | Directories sorted first; sizes shown for files |
| `grep` | Pure-Python regex over `Path.rglob`; skips `DEFAULT_EXCLUDES` directories, files > 2 MB, and binary files (same decoding as `read_file`); caps at `max_results` (1–1000) |
| `fetch_url` | http(s) only; 100 KB default cap (clamped 1 KB–1 MB); HTTP errors returned as text, connection errors as `ToolError` |
| `write_file` | Creates parent directories; overwrites |
| `edit_file` | Exact string match; refuses empty `old_string`; fails on 0 matches or >1 without `replace_all` |
| `run_command` | `subprocess.run(shell=True, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")`, `cwd=workspace`, timeout defaults to 60 s and is clamped 1–600 s; stdout/stderr merged with an `--- stderr ---` marker; exit code reported |

### Workspace boundary

`resolve_path` (`agent.py:1183`) resolves every file-tool path (relative to the
workspace), follows symlinks via `Path.resolve`, and requires the result to be
inside `config.workspace`. Shell commands are not path-constrained, but are
constrained by approval and run with `cwd=workspace`.

### Output caps

- `TOOL_RESULT_LIMIT` (64,000 characters) — what the model sees and what is
  logged.
- `DISPLAY_PREVIEW_LIMIT` (2,000 characters) / `DISPLAY_PREVIEW_LINES` (10) —
  terminal preview only; the model still receives the full capped result.
- Truncation always leaves an explicit `...[truncated N chars]` marker.

## 8. Mode enforcement

PLAN mode is enforced in three independent layers:

1. **Tool schema filtering** — `App.allowed_tool_schemas` (`agent.py:2688`)
   omits write tools, so the model is never told they exist.
2. **Dispatcher guard** — `execute_tool` rejects write tools in PLAN mode.
3. **System prompt** — `build_system_prompt` (`agent.py:1681`) states the mode
   and its rules, reducing wasted attempts.

The mode is changed only by `App.toggle_mode` (`agent.py:2702`), bound to `Tab`,
`/mode`, and `/init` (which switches to BUILD before running its turn). Toggling
rebuilds `messages[0]` and writes a `mode_change` audit event. Because input is
only read at the prompt, a turn always runs in a single mode.

## 9. Approval state machine

`Approvals` (`agent.py:1729`) holds `allowed: set[str]` (exact command strings).
The set is empty for now; it is the seam for a future config-file allowlist.

```
                 ┌───────────────────────────────┐
                 │ run_command requested         │
                 └──────────────┬────────────────┘
                                │ command in allowed?
                     yes ┌──────┴──────┐ no
                         ▼             ▼
                    execute     show command + cwd + Yes/No buttons
                                      │
                     Enter on Yes ────┤ execute
                     Enter on No ─────┤ deny  → "User denied execution..."
                     Esc ─────────────┘ cancel turn → clean prompt
```

Properties:

- The displayed command is byte-for-byte what is passed to the shell.
- The widget is a small inline `prompt_toolkit` `Application` with a
  `FormattedTextControl(show_cursor=False)`, reusing the shared session's
  input/output. `←`/`→` move the highlight, `Enter` confirms; `Yes` is the
  default. It does not share the main prompt's `Tab` binding.
- The allowlist is exact-match only (no prefix or wildcard rules) and is empty
  until config-file support lands, so every command is approved per request.
- Denials are returned to the model as a tool result so it can propose an
  alternative.
- Every decision is logged as an `approval` event with the command; the
  `decision` is `allow`, `deny`, or `cancel`.

## 10. Audit log

`AuditLog` (`agent.py:528`) opens `<app dir>/logs/YYYYMMDD-HHMMSS-<pid>.jsonl`
(UTC) at startup, appends one JSON object per event, and flushes after every
write. If the default app-directory location is not writable, a warning is
printed and the session continues without logging.

Event schema (common fields: `ts` in UTC ISO-8601, `event`):

| Event | Extra fields |
| --- | --- |
| `session_start` | `version`, `base_url`, `model`, `api_style`, `workspace`, `mode`, `api_key` (redacted), `instructions`, `instructions_bytes`, `context_window`, `context_window_source` |
| `user_message` | `content` |
| `assistant_message` | `content`, `tool_calls` |
| `tool_call` | `name`, `arguments` (full object), `call_id` |
| `approval` | `command`, `decision` (`allow`/`deny`/`cancel`) |
| `tool_result` | `name`, `call_id`, `ok`, `content` (capped) |
| `mode_change` | `mode` |
| `error` | `message` |
| `session_end` | — |

Tool results are capped before logging (64,000 characters) to bound file growth;
everything else is stored in full. Serialisation uses `default=str`, so
unexpected types are stored as strings.

## 11. Error handling model

| Failure | Handling |
| --- | --- |
| Server unreachable / HTTP error | `AgentError` → printed, logged as `error`, turn ends; history intact |
| Non-SSE body with `error` field | `AgentError` with the server message |
| Malformed tool arguments | `ERROR:` tool result; model can retry |
| `ToolError` from a handler | `ERROR:` tool result |
| Unexpected handler exception | `ERROR: <tool> failed: ...` tool result |
| `Esc` while streaming | Partial reply kept; tool calls dropped |
| `Esc` at the approval prompt | Turn cancelled (`approval: cancel`); the dangling assistant tool-call step is dropped from history |
| `Ctrl+C` during a command | BA quits (child receives SIGINT) |
| `Ctrl+C` elsewhere in a turn | Quits `main` cleanly via `KeyboardInterrupt`/`QuitApp` |
| Step limit reached | Warning printed and logged |

The guiding rule: **expected user-visible failures are reported without
unwinding the turn, and tool failures are returned to the model as text**, never
raised. Unexpected exceptions still terminate the REPL through `main`'s `finally`
block.

## 12. Extension points

**Adding a tool** requires up to four edits in `agent.py`:

1. Write a handler `def tool_foo(config: Config, ...) -> str` that raises
   `ToolError` for expected failures.
2. Add a `Tool(...)` entry in `build_tools` with its JSON schema, setting
   `writes=True` if it modifies the host (which also disables it in PLAN mode)
   and `requires_approval=True` if a human must confirm it.
3. If it requires approval, extend `execute_tool`'s approval branch (currently
   keyed on `args["command"]`) or generalize the gate to a per-tool callback.
4. Mention it in `build_system_prompt` if the model needs usage guidance.

**Changing the TUI** means editing the style sheet (`agent.py:244-293`), `say` /
`stream_write`, or `build_toolbar`.

**Adding a slash command** is a branch in `handle_command`
(`agent.py:2547`). `/init` is the exception that runs a full turn: it loads
`config/prompts/init.md` (`load_init_prompt`, `agent.py:2515`), switches to
BUILD, feeds the prompt through `run_turn`, then reloads `Agents.md`
(`reload_project_instructions`, `agent.py:2532`).

## 13. Testing strategy

There is no committed automated test suite. CI runs the pip smoke test, the
dev-shell `--version` smoke test, `ruff check .`, and a check that
`config/prompts/init.md` exists.

Static checks (the first two are provided by the dev shell):

```sh
nix develop -c python -m py_compile agent.py
nix develop -c ruff check agent.py
nix run nixpkgs#actionlint -- .github/workflows/release.yml
```

For manual testing, run the scripted mock in `demo/mock_llm.py` and point BA at
`--base-url http://127.0.0.1:<port>/v1`. The demo tapes use the same mock.

## 14. Design decisions and tradeoffs

| Decision | Rationale | Tradeoff |
| --- | --- | --- |
| Single file, 2,902 lines | Auditable in one pass; easy to copy to a Windows box | Less modular; large diffs |
| stdlib `urllib` for HTTP/SSE | No required dependency beyond the TUI; every byte of wire handling is visible | Manual SSE parsing; no HTTP/2, connection pooling, or general retries |
| `prompt_toolkit` over Textual/curses | Cross-platform, linear synchronous usage, built-in key bindings and toolbar | Plain scrollback instead of panes/modals |
| Synchronous linear loop | Easiest possible control flow to reason about | No streaming input; the UI is blocked during a turn |
| Native `tool_calls` only | Standard protocol; no brittle text parsing | Requires server-side function-calling support |
| PLAN mode default | The first action of a fresh session can never modify the host | One extra Tab before editing |
| Exact-match command allowlist | Auditable and conservative; no persisted policy to drift | Every command is re-approved per request (config-file allowlist planned) |
| Internal streaming markdown renderer | No new parsing dependency; keeps token-by-token prose and the prompt_toolkit output path | Hand-rolled subset: no nested emphasis, per-line code lexing, tables buffered |
| Server `usage` + byte estimate for context | Exact when the server reports usage; always something to show otherwise | Numbers switch between exact and estimated; `include_usage` is dropped after HTTP 400 |
| Context window probe at startup | Zero-config denominator for the toolbar | Two 5 s best-effort requests (`/props`, `/models`); unknown if the server hides it |
| Summary model for commands | Plain-language effect shown before approval; can run on a small cheap model | Extra request per unique command; command text goes to the summary endpoint |
| Workspace path boundary | Prevents accidental reads/writes outside the project | No escape hatch; copy files in or point `--workspace` at a parent |
| Full history, no compaction | Simple and lossless; local models often have large contexts | Very long sessions can exceed the model's context window |
| JSONL audit log | Grep-able, append-only, crash-safe with per-event flush | Unbounded growth; external rotation needed |

## 15. Known limitations and future work

- No context compaction; the toolbar shows exact server `usage` when available
  and a byte-based estimate otherwise, but long sessions will still eventually
  overflow the model context.
- No session persistence or resume; `/clear` is destructive.
- No general retry/backoff on transient network failures; only the explicit
  one-shot fallbacks in §6.
- `fetch_url` does not block private/link-local addresses (the agent is intended
  for trusted LANs).
- Markdown rendering is a pragmatic subset: emphasis cannot be nested, fenced
  code is lexed line by line (multiline constructs may not highlight), and
  tables are emitted only after the block ends.
- The approval gate is specific to `run_command`; other tools that might deserve
  approval (e.g. `write_file`) are gated only by mode.
- No committed automated test suite; the release workflow smoke-tests the
  bundled agent (`import prompt_toolkit`, `--version`, `--help`, and
  `ba.cmd --version`) but does not exercise the TUI or the agent loop.

## 16. Packaging and release

The Windows release pipeline is `.github/workflows/release.yml`, triggered by
`v*` tag pushes (publishes a GitHub Release) or manual dispatch (workflow
artifact only). There is no separate local packaging step.

```
tag vX.Y.Z ──► validate tag == VERSION in agent.py
           ──► download python-build-standalone 20261003
               (cpython-3.14.8 x86_64-pc-windows-msvc install_only_stripped)
           ──► verify SHA-256 against the release's SHA256SUMS asset
           ──► extract (python/) ──► python -m pip install -r requirements.txt
           ──► copy agent.py, README.md, ba.cmd, config/prompts/init.md
           ──► smoke test: import prompt-toolkit, agent --version/--help,
               ba.cmd --version
           ──► 7z zip (contents at zip root) + root-entry assertions
               (including config/prompts/init.md)
           ──► SHA-256 file ──► upload artifact ──► gh release create
```

Design notes:

- **Tag is the version authority.** `VERSION` in `agent.py` is the single source
  of truth; the build fails on a mismatch rather than rewriting source in CI.
- **Supply-chain check.** The interpreter archive is verified against the
  checksum published by python-build-standalone, and the resulting zip gets its
  own `.sha256` asset (`<hash>  <filename>`).
- **Direct interpreter invocation.** The workflow and `ba.cmd` invoke
  `python\python.exe` directly (`-m pip`, `agent.py`), so they do not rely on
  pip script shims.
- **The smoke test runs the assembled package** with the bundled interpreter
  (`agent.py --version/--help`) and through the `ba.cmd` launcher, and
  the zip's root entries are asserted after archiving.
- **A tag push is the only path that publishes.** The release step is gated on
  `github.event_name == 'push' && github.ref_type == 'tag'`; manual dispatch runs
  build and upload the workflow artifact only, even when dispatched against a
  tag ref.
- **`install_only_stripped`** is the pinned PBS flavor, set in `env` alongside
  the release and interpreter versions.
- **Pins are explicit** in the workflow `env` and bumped deliberately;
  Dependabot manages actions, `requirements.txt` and `flake.lock` but not these
  values.
- **Bundle layout** — `agent.py`, `ba.cmd`, `README.md`,
  `config/prompts/init.md` and `python/` (interpreter + site-packages) at the
  zip root; users run `ba.cmd`.

