# BasicAgent

A basic LLM coding agent built for **understandability and auditability**. It is a
single Python file (`agent.py`) that provides a cross-platform terminal UI, talks to
any **OpenAI-compatible API on your LAN**, and requires **explicit user approval for
every shell command** it wants to run.

```
+-------------------------------------------------------------+
| PLAN | qwen2.5-coder  /home/you/project  Tab:mode  /help     |
+-------------------------------------------------------------+
you> explain the build setup
BasicAgent [plan]> This project uses a Nix flake...
```

## Features

- **Single file** — `agent.py` is the whole agent; stdlib HTTP/SSE plus
  `prompt_toolkit` for the TUI.
- **Streaming** — responses are rendered token-by-token as they arrive.
- **PLAN / BUILD modes** — press `Tab` to toggle. PLAN is read-only and enforced
  structurally (write/run tools are not even offered to the model). BUILD enables
  file edits and shell commands.
- **Approval gate** — every shell command is shown to you first:
  run once (`y`), always this session (`a`), or deny (`n`).
- **Audit log** — every message, tool call, approval and result is appended to a
  JSONL file for later review.
- **Small tool surface** — `read_file`, `list_dir`, `grep`, `fetch_url`,
  `write_file`, `edit_file`, `run_command`.

## Requirements

- Python 3.10+ with `prompt_toolkit`
- An OpenAI-compatible chat completions endpoint that supports **native tool
  calling** (function calling), e.g. llama.cpp server with `--jinja`, vLLM,
  LM Studio, or similar.
- Linux: Nix with flakes (this repository ships a `flake.nix`).
- Windows: Python from python.org plus `pip install prompt-toolkit`.

## Setup

### Linux (NixOS)

```sh
nix develop
python agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder
```

The dev shell provides Python with `prompt-toolkit` and `ruff`. No venv or `pip`
is required. `flake.lock` pins nixpkgs for reproducibility.

### Windows

```powershell
py -m pip install -r requirements.txt
py agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder
```

Use Windows Terminal for the best rendering.

## Usage

```sh
# Minimal
python agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder

# Via environment variables
export AGENT_BASE_URL=http://192.168.1.10:8080/v1
export AGENT_MODEL=qwen2.5-coder
python agent.py

# Run against a subdirectory, with no audit log
python agent.py --workspace ~/src/myproject --no-log
```

### Keyboard

| Key | Action |
| --- | --- |
| `Tab` | Toggle PLAN / BUILD mode |
| `Ctrl+C` | At the prompt: clear the line. While streaming: interrupt the response |
| `Ctrl+D` | Quit |

### Slash commands

| Command | Description |
| --- | --- |
| `/help` | Show available commands |
| `/clear` | Clear the conversation (keeps the system prompt) |
| `/mode` | Toggle PLAN/BUILD (same as `Tab`) |
| `/tools` | List tools available in the current mode |
| `/log` | Show the path of the current audit log |
| `/exit` | Quit (also `/quit`) |

## Modes

| Mode | Purpose | Tools available |
| --- | --- | --- |
| **PLAN** (default) | Inspect and plan; no changes to the host | `read_file`, `list_dir`, `grep`, `fetch_url` |
| **BUILD** | Implement changes | all tools; shell still requires approval |

The mode is shown in the status bar and is also written into the system prompt.
Switching modes rebuilds the system prompt; the mode is fixed for the duration of
a turn.

## Approval

When the model requests `run_command`, the agent pauses and shows the exact command
and working directory:

```
+-- run_command -----------------------------------------------
| cwd: /home/you/project
| $ git status
+--------------------------------------------------------------
Run? [y] once  [a] always (session)  [n] deny:
```

- `y` — run this one time.
- `a` — remember this exact command for the rest of the session
  (exact string match; in memory only, never persisted).
- `n` (or empty, or `Ctrl+C`) — deny; the model is told the user denied it.

The command is executed exactly as shown, via the system shell, in the workspace
directory. There is no hidden wrapping or rewriting.

## Tools

| Tool | Mode | Description |
| --- | --- | --- |
| `read_file(path, start_line?, end_line?)` | both | Numbered lines, max 2000 lines per call, refuses binary files |
| `list_dir(path?)` | both | Directory listing; directories get a trailing `/` |
| `grep(pattern, path?, include?, ignore_case?, max_results?)` | both | Regex search with line numbers, skips `.git`, `node_modules`, `.venv`, caches |
| `fetch_url(url, max_bytes?)` | both | HTTP(S) GET, capped at 100 KB by default |
| `write_file(path, content)` | build | Create/overwrite; creates parent directories |
| `edit_file(path, old_string, new_string, replace_all?)` | build | Exact-string replacement; fails on ambiguity |
| `run_command(command, timeout_seconds?)` | build | Shell command; **always requires approval** |

Tool results are capped at 64 KB sent to the model and previewed at 2 KB in the
terminal; the full (capped) result is stored in the audit log. File tools are
confined to the workspace unless `--allow-outside` is given.

## Configuration

Precedence: CLI argument > environment variable > default.

| CLI | Environment | Default | Meaning |
| --- | --- | --- | --- |
| `--base-url` | `AGENT_BASE_URL`, `OPENAI_BASE_URL` | `http://localhost:8080/v1` | API root |
| `--api-key` | `AGENT_API_KEY`, `OPENAI_API_KEY` | `none` | Bearer token |
| `--model` | `AGENT_MODEL`, `OPENAI_MODEL` | `local-model` | Model name |
| `--temperature` | — | unset | Omitted from requests unless given |
| `--max-tokens` | — | unset | Omitted from requests unless given |
| `--timeout` | — | `120` | HTTP timeout (seconds) |
| `--command-timeout` | — | `60` | Default shell timeout (seconds, clamped 1–600) |
| `--max-steps` | — | `25` | Max tool rounds per user turn |
| `--workspace` | — | current directory | Workspace root for tools and shell |
| `--log-dir` | — | `logs` | Audit log directory |
| `--no-log` | — | off | Disable audit logging |
| `--allow-outside` | — | off | Allow file tools outside the workspace |
| `--system-prompt` | — | built-in | Replace the base system prompt |

## Audit log

Unless `--no-log` is passed, each session writes
`logs/YYYYMMDD-HHMMSS-<pid>.jsonl` (UTC timestamp). One JSON object per line:

| Event | Contents |
| --- | --- |
| `session_start` | version, base URL, model, workspace, mode, redacted API key |
| `user_message` | user input |
| `assistant_message` | assistant text and any tool calls |
| `tool_call` | tool name, full arguments, call id |
| `approval` | command and decision (`allow` / `always` / `deny`) |
| `tool_result` | call id, success flag, capped output |
| `mode_change` | new mode |
| `error` | agent or step-limit errors |
| `session_end` | written on exit |

## Security notes

- The API key is never logged; it is recorded as `[redacted]`.
- Shell commands are displayed verbatim before execution and never rewritten.
- The session allowlist is exact-match and in-memory only.
- File tools are confined to the workspace by default.
- Tool output sent to the model is truncated to protect the context window; the
  truncation marker is explicit.

## Troubleshooting

- **The model never calls tools** — your server/model must support native OpenAI
  tool calling. For llama.cpp, start the server with `--jinja`.
- **`cannot reach http://...`** — check the base URL and that the server is
  reachable from this machine (`curl <base-url>/models`).
- **The agent refuses to write files** — you are in PLAN mode; press `Tab`.
- **Garbled output on Windows** — use Windows Terminal; legacy `conhost` has
  limited Unicode/ANSI support.

## Project layout

```
agent.py          the entire agent
flake.nix         nix develop environment (Python + prompt-toolkit + ruff)
flake.lock        pinned nixpkgs
requirements.txt  runtime dependencies for pip (Windows / non-Nix)
ARCHITECTURE.md   internal design and data flow
```
