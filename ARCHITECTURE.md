# Architecture

This document describes the internal design of BasicAgent: its components, the
data flow of a turn, and the reasoning behind the main decisions. It is intended
for anyone auditing or extending `agent.py`.

The entire implementation lives in one file, `agent.py` (~1040 lines). Section
markers in that file match the headings below. Line references are approximate
and refer to the current revision.

## 1. Goals and non-goals

**Goals**

1. **Understandable** — a linear, synchronous control flow that can be read
   top-to-bottom in one sitting. No frameworks, no plugin system, no hidden state.
2. **Auditable** — every exchange, tool call, approval decision and result is
   written to a JSONL log. Shell commands are displayed exactly as executed.
3. **Safe by construction** — PLAN mode physically cannot modify the host, and
   BUILD mode cannot run a shell command without explicit user consent.
4. **Cross-platform** — one file runs on Linux and Windows with a single
   third-party dependency (`prompt_toolkit`).

**Non-goals (for now)**

- Multi-agent orchestration, MCP, embeddings, or RAG.
- Session persistence/resume and context compaction.
- Sandboxing the shell beyond user approval and a working directory.
- Automatic retries, fallback models, or provider-specific features.

## 2. Component map

| Component | Location | Responsibility |
| --- | --- | --- |
| Constants | `agent.py:32-65` | Limits and the prompt_toolkit style sheet |
| `AgentError` / `ToolError` | `agent.py:67-72` | User-facing failure vs. model-visible tool failure |
| `Mode` | `agent.py:75` | `PLAN` / `BUILD` enum |
| `Config` | `agent.py:81` | All runtime settings |
| `parse_args` | `agent.py:97` | CLI parsing, env fallbacks, precedence |
| `AuditLog` | `agent.py:162` | Append-only JSONL writer |
| `say` / `stream_write` / `cap` | `agent.py:191-202` | Terminal rendering and output truncation |
| `Tool` | `agent.py:211` | Tool metadata: schema, handler, flags |
| `resolve_path` / `is_excluded` | `agent.py:235-258` | Workspace boundary and directory excludes |
| `tool_*` handlers | `agent.py:261-451` | The seven tools |
| `build_tools` | `agent.py:454` | Registry: name → `Tool` |
| `build_system_prompt` | `agent.py:552` | Mode-aware system prompt |
| `Approvals` | `agent.py:586` | Interactive shell-command gate + session allowlist |
| `StreamResult` | `agent.py:627` | One assistant reply: message dict + interrupted flag |
| `parse_non_stream_response` | `agent.py:632` | Fallback for servers that ignore `stream: true` |
| `finalize_tool_calls` | `agent.py:662` | Merge streamed tool-call fragments |
| `stream_chat` | `agent.py:679` | HTTP POST + SSE parsing + live rendering |
| `summarize_args` | `agent.py:778` | One-line tool-call display |
| `execute_tool` | `agent.py:793` | Dispatch pipeline: validate → guard → approve → run → log |
| `run_turn` | `agent.py:850` | One user turn: stream → tools → repeat |
| `handle_command` | `agent.py:892` | Slash commands |
| `App` | `agent.py:932` | Wires config, log, session, mode, messages, tools, approvals |
| `build_toolbar` / `build_key_bindings` | `agent.py:960-980` | Status bar and Tab binding |
| `main` | `agent.py:992` | Banner, REPL loop, shutdown |

## 3. State model

`App` (`agent.py:932`) owns all mutable runtime state:

```python
class App:
    config: Config
    log: AuditLog
    session: PromptSession
    mode: Mode                      # starts as PLAN
    messages: list[dict]            # OpenAI chat messages; messages[0] is system
    tools: dict[str, Tool]
    approvals: Approvals            # session allowlist
```

`messages` is the single source of conversation truth and is sent verbatim to the
server each request. The system prompt at `messages[0]` is rebuilt whenever the
mode changes (`App.toggle_mode`, `agent.py:945`), so mode instructions always
reflect the current mode. `/clear` resets `messages` to just the system prompt.

Configuration is immutable after startup. Precedence is CLI > environment >
default, resolved entirely in `parse_args` (`agent.py:97`). There is no config
file; environment variables are the intended way to set machine-specific values
such as `AGENT_BASE_URL`.

## 4. Startup

`main` (`agent.py:992`):

1. Parse configuration.
2. Open the audit log (unless disabled) and write `session_start` with a
   redacted API key.
3. Create the `PromptSession` with the shared style sheet, the `App`, and the
   Tab key binding.
4. Print the banner (endpoint, model, workspace, log path, current mode).
5. Enter the REPL loop.

The REPL loop calls `session.prompt(...)` with the Tab binding and a
`bottom_toolbar` callable. `Ctrl+C` at the prompt raises `KeyboardInterrupt` and
is caught to continue; `Ctrl+D` raises `EOFError` and exits. Input starting with
`/` goes to `handle_command`; everything else goes to `run_turn`. The loop ends
via `/exit`, `EOFError`, or an uncaught exception, and `session_end` is always
written in a `finally` block.

## 5. Turn lifecycle

`run_turn` (`agent.py:850`) implements the agent loop. One user turn:

```
user text
   │
   ├─ append {"role":"user"} ──────────────────────────────► audit: user_message
   │
   └─ repeat up to config.max_steps times:
        │
        ├─ stream_chat(app, stream_write) ──► POST {base_url}/chat/completions
        │      │                                stream: true, tools: allowed schemas
        │      │
        │      ├─ SSE content deltas ──────► rendered immediately
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
             │    ├─ needs approval & unseen? → Approvals.ask ──► audit: approval
             │    │      └─ denied           → ERROR result
             │    ├─ run handler              ─────────────────► audit: tool_call
             │    └─ cap + log result         ─────────────────► audit: tool_result
             └─ append {"role":"tool", "tool_call_id", "content"}
```

If the loop exhausts `max_steps`, a warning is printed and logged, and the turn
ends. This bounds runaway tool loops.

### Interruption

`Ctrl+C` while streaming raises `KeyboardInterrupt` inside `stream_chat`. The
partial text is kept, `[interrupted by user]` is appended, and `StreamResult`
sets `interrupted=True`. `run_turn` then returns without executing any
partially-received tool calls (their argument JSON would be incomplete and
possibly dangerous to guess at).

## 6. LLM client

`stream_chat` (`agent.py:679`) uses only `urllib.request` and `json` — no HTTP
library.

**Request**

```json
{
  "model": "<config.model>",
  "messages": [ ... ],
  "stream": true,
  "tools": [ ... ],        // only when the mode permits at least one tool
  "tool_choice": "auto",
  "temperature": ...,      // only when set
  "max_tokens": ...        // only when set
}
```

`Authorization: Bearer <api_key>` is always sent; LAN servers that ignore auth
accept the default `none`.

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

`finalize_tool_calls` (`agent.py:662`) converts the accumulator into the exact
OpenAI `tool_calls` shape, defaulting empty argument strings to `{}`. The
assistant message is then appended verbatim so the subsequent `role: "tool"`
messages reference valid `tool_call_id`s.

**Non-stream fallback.** If the response `Content-Type` is not
`text/event-stream`, the body is parsed as a normal completion by
`parse_non_stream_response` (`agent.py:632`). This covers servers that silently
ignore `stream: true`, and surfaces `{"error": ...}` bodies as `AgentError`.

**No retries.** A failed request raises `AgentError`, which `run_turn` prints and
logs; the user's message stays in history, so the user can simply retry.

## 7. Tool system

Each tool is a `Tool` dataclass (`agent.py:211`):

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

`build_tools` (`agent.py:454`) closes over `Config` with lambdas so handlers are
plain functions with keyword arguments. `tool_schema` converts a `Tool` into the
OpenAI function schema.

**Dispatch pipeline** (`execute_tool`, `agent.py:793`), in order:

1. Parse `function.arguments` as JSON. Malformed JSON returns an `ERROR:` string
   rather than raising, so the model can correct itself.
2. Reject unknown tool names.
3. **Mode guard** — a `writes` tool in PLAN mode is refused, even if the model
   somehow requests it. This is the second enforcement layer (the first is not
   sending the schema at all).
4. **Approval gate** — `requires_approval` tools (only `run_command`) are checked
   against the session allowlist; if absent, `Approvals.ask` runs.
5. Log `tool_call` with full arguments.
6. Execute the handler. `ToolError` becomes `ERROR: <message>`; `TypeError`
   (bad argument names/types) and unexpected exceptions are converted likewise,
   so a buggy tool can never crash the agent loop.
7. Cap the result and log `tool_result`.

Handlers always return strings. Errors are strings starting with `ERROR:`,
which the system prompt tells the model to interpret as failure.

### Per-tool behavior and limits

| Tool | Implementation notes |
| --- | --- |
| `read_file` | Binary detection (NUL byte in first 8 KB); UTF-8 with replacement; numbered lines; `READ_MAX_LINES` (2000) cap with an explicit header note |
| `list_dir` | Directories sorted first; sizes shown for files |
| `grep` | Pure-Python regex over `Path.rglob`; skips `DEFAULT_EXCLUDES` directories and files > 2 MB; caps at `max_results` (1–1000) |
| `fetch_url` | http(s) only; 100 KB default cap (clamped 1 KB–1 MB); HTTP errors returned as text, connection errors as `ToolError` |
| `write_file` | Creates parent directories; overwrites |
| `edit_file` | Exact string match; refuses empty `old_string`; fails on 0 matches or >1 without `replace_all` |
| `run_command` | `subprocess.run(shell=True, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")`, `cwd=workspace`, timeout clamped 1–600 s; stdout/stderr merged with an `--- stderr ---` marker; exit code reported |

### Workspace boundary

`resolve_path` (`agent.py:235`) resolves every file-tool path (relative to the
workspace), follows symlinks via `Path.resolve`, and requires the result to be
inside `config.workspace` unless `--allow-outside` is set. Shell commands are not
path-constrained, but are constrained by approval and run with `cwd=workspace`.

### Output caps

- `TOOL_RESULT_LIMIT` (64 KB) — what the model sees and what is logged.
- `DISPLAY_PREVIEW_LIMIT` (2 KB) / `DISPLAY_PREVIEW_LINES` (40) — terminal
  preview only; the model still receives the full capped result.
- Truncation always leaves an explicit `...[truncated N chars]` marker.

## 8. Mode enforcement

PLAN mode is enforced in three independent layers:

1. **Tool schema filtering** — `App.allowed_tool_schemas` (`agent.py:947`)
   omits write tools, so the model is never told they exist.
2. **Dispatcher guard** — `execute_tool` rejects write tools in PLAN mode.
3. **System prompt** — `build_system_prompt` (`agent.py:552`) states the mode and
   its rules, reducing wasted attempts.

The mode is changed only by `App.toggle_mode` (`agent.py:950`), bound to `Tab`
and `/mode`. Toggling rebuilds `messages[0]` and writes a `mode_change` audit
event. Because input is only read at the prompt, a turn always runs in a single
mode.

## 9. Approval state machine

`Approvals` (`agent.py:586`) holds `allowed: set[str]` (exact command strings).

```
                 ┌───────────────────────────────┐
                 │ run_command requested         │
                 └──────────────┬────────────────┘
                                │ command in allowed?
                     yes ┌──────┴──────┐ no
                         ▼             ▼
                    execute     show command + cwd
                                      │
                      y ──────────────┤ execute
                      a ──────────────┤ add to allowed, execute
                      n / empty ──────┤ deny  → "User denied execution..."
                      Ctrl+C/EOF ─────┘ deny
```

Properties:

- The displayed command is byte-for-byte what is passed to the shell.
- The allowlist is exact-match only (no prefix or wildcard rules), in memory
  only, and never persisted.
- Denials are returned to the model as a tool result so it can propose an
  alternative.
- Every decision is logged as an `approval` event with the command.

The approval prompt is a second `PromptSession.prompt` call with empty key
bindings, so `Tab` cannot accidentally change mode mid-turn.

## 10. Audit log

`AuditLog` (`agent.py:162`) opens `logs/YYYYMMDD-HHMMSS-<pid>.jsonl` (UTC) at
startup, appends one JSON object per event, and flushes after every write. If
logging is disabled (`--no-log`), all methods are no-ops.

Event schema (common fields: `ts` in UTC ISO-8601, `event`):

| Event | Extra fields |
| --- | --- |
| `session_start` | `version`, `base_url`, `model`, `workspace`, `mode`, `api_key` (redacted) |
| `user_message` | `content` |
| `assistant_message` | `content`, `tool_calls` |
| `tool_call` | `name`, `arguments` (full object), `call_id` |
| `approval` | `command`, `decision` (`allow`/`always`/`deny`) |
| `tool_result` | `name`, `call_id`, `ok`, `content` (capped) |
| `mode_change` | `mode` |
| `error` | `message` |
| `session_end` | — |

Tool results are capped before logging (64 KB) to bound file growth; everything
else is stored in full. Serialization uses `default=str` so unexpected types can
never break logging.

## 11. Error handling model

| Failure | Handling |
| --- | --- |
| Server unreachable / HTTP error | `AgentError` → printed, logged as `error`, turn ends; history intact |
| Non-SSE body with `error` field | `AgentError` with the server message |
| Malformed tool arguments | `ERROR:` tool result; model can retry |
| `ToolError` from a handler | `ERROR:` tool result |
| Unexpected handler exception | `ERROR: <tool> failed: ...` tool result |
| `KeyboardInterrupt` during streaming | Partial reply kept; tool calls dropped |
| `KeyboardInterrupt` during a command | `ERROR: command interrupted by user` |
| `KeyboardInterrupt` elsewhere in a turn | Caught in `main`; warning printed |
| Step limit reached | Warning printed and logged |

The guiding rule: **user-visible failures never crash the REPL, and tool
failures are always returned to the model as text**, never raised.

## 12. Extension points

**Adding a tool** requires four edits in `agent.py`:

1. Write a handler `def tool_foo(config: Config, ...) -> str` that raises
   `ToolError` for expected failures.
2. Add a `Tool(...)` entry in `build_tools` with its JSON schema, setting
   `writes=True` if it modifies the host (which also disables it in PLAN mode)
   and `requires_approval=True` if a human must confirm it.
3. If it requires approval, extend `execute_tool`'s approval branch (currently
   keyed on `args["command"]`) or generalize the gate to a per-tool callback.
4. Mention it in `build_system_prompt` if the model needs usage guidance.

**Changing the TUI** means editing the style sheet (`agent.py:51`), `say` /
`stream_write`, or `build_toolbar`.

**Adding a slash command** is a branch in `handle_command` (`agent.py:892`).

## 13. Testing strategy

There is no committed test suite; the agent was verified with throwaway scripts
in `/tmp/opencode`:

- `test_agent.py` — a mock OpenAI SSE server (stdlib `http.server`) that emits
  fragmented tool-call deltas, plus assertions covering: PLAN-mode schema
  filtering, PLAN-mode write blocking, streamed tool-call assembly, approval
  `y`/`a`/`n` behavior, all file tools, the workspace boundary, `fetch_url`, a
  full turn, server-visible tool lists, and audit-log events.
- `test_tui.py` — uses `prompt_toolkit`'s `create_pipe_input`/`DummyOutput` to
  verify `Tab` toggles PLAN↔BUILD, the system prompt updates, mode changes are
  logged, and `Ctrl+D` exits.

Static checks, both provided by the dev shell:

```sh
nix develop -c python -m py_compile agent.py
nix develop -c ruff check agent.py
```

To test manually, point the agent at a LAN endpoint or run the mock server and
use `--base-url http://127.0.0.1:<port>/v1`.

## 14. Design decisions and tradeoffs

| Decision | Rationale | Tradeoff |
| --- | --- | --- |
| Single file, ~1k lines | Auditable in one pass; easy to copy to a Windows box | Less modular; large diffs |
| stdlib `urllib` for HTTP/SSE | No dependency beyond the TUI; every byte of wire handling is visible | Manual SSE parsing; no HTTP/2, connection pooling, or retries |
| `prompt_toolkit` over Textual/curses | Cross-platform, linear synchronous usage, built-in key bindings and toolbar | Plain scrollback instead of panes/modals |
| Synchronous linear loop | Easiest possible control flow to reason about | No streaming input; the UI is blocked during a turn |
| Native `tool_calls` only | Standard protocol; no brittle text parsing | Requires server-side function-calling support |
| PLAN mode default | The first action of a fresh session can never modify the host | One extra Tab before editing |
| Exact-match, in-memory allowlist | Auditable and conservative; no persisted policy to drift | Re-approval across sessions; repeated long commands need `a` |
| Workspace path boundary | Prevents accidental reads/writes outside the project | Requires `--allow-outside` for legitimate external paths |
| Full history, no compaction | Simple and lossless; local models often have large contexts | Very long sessions can exceed the model's context window |
| JSONL audit log | Grep-able, append-only, crash-safe with per-event flush | Unbounded growth; external rotation needed |

## 15. Known limitations and future work

- No context compaction or token accounting; long sessions will eventually
  overflow the model context.
- No session persistence or resume; `/clear` is destructive.
- No retry/backoff on transient network failures.
- `fetch_url` does not block private/link-local addresses (the agent is intended
  for trusted LANs).
- The approval gate is specific to `run_command`; other tools that might deserve
  approval (e.g. `write_file`) are gated only by mode.
- No Windows CI; cross-platform behavior relies on stdlib and `prompt_toolkit`.
