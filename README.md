# BasicAgent (BA)

<p align="center">
  <img src="media/BA.png" alt="BA" width="600">
</p>

A basic LLM coding agent built for **understandability and auditability**. It is a
single Python file (`agent.py`) that provides a cross-platform terminal UI, talks to
any **OpenAI-compatible API on your LAN**, and requires **explicit user approval for
every shell command** it wants to run.

```
+-------------------------------------------------------------+
| PLAN | qwen2.5-coder  /home/you/project  Tab:mode  /help     |
+-------------------------------------------------------------+
you> explain the build setup
BA [plan]> This project uses a Nix flake...
```

## Features

- **Single file** — `agent.py` is the whole agent; stdlib HTTP/SSE plus
  `prompt_toolkit` for the TUI.
- **Streaming** — responses are rendered token-by-token as they arrive.
- **Markdown rendering** — assistant replies render headings, emphasis, code
  fences (syntax-highlighted when `pygments` is installed), lists, task lists,
  tables and links while they stream.
- **Wait indicator** — an ASCII spinner is shown while BA is waiting for the
  model's first output of a turn.
- **Context usage** — the status bar shows tokens used against the context
  window (probed from the server, or set with `AGENT_CONTEXT_WINDOW`). Exact when
  the server reports `usage`, estimated otherwise.
- **PLAN / BUILD modes** — press `Tab` to toggle. PLAN is read-only and enforced
  structurally (write/run tools are not even offered to the model). BUILD enables
  file edits and shell commands.
- **Approval gate** — every shell command is shown to you first, with Yes/No
  buttons (`←`/`→` then `Enter`) before it runs.
- **Command summaries** — before approval, a second (optionally different) model
  explains each shell command in one line for a junior developer.
- **Audit log** — every message, tool call, approval and result is appended to a
  JSONL file for later review.
- **Small tool surface** — `read_file`, `list_dir`, `grep`, `fetch_url`,
  `write_file`, `edit_file`, `run_command`.
- **Project instructions** — an `Agents.md` in the workspace root (any
  capitalization) is loaded into the system prompt at startup.
- **Startup instructions** — an `Instructions.md` next to `agent.py` is rendered
  as markdown right after the banner, if present.

## Requirements

- Python 3.10+ with `prompt_toolkit` (plus `pygments` for code-block highlighting)
- An OpenAI-compatible chat completions endpoint that supports **native tool
  calling** (function calling), e.g. llama.cpp server with `--jinja`, vLLM,
  LM Studio, or similar.
- Linux: Nix with flakes (this repository ships a `flake.nix`).
- Windows: Python from python.org plus `pip install -r requirements.txt`.

## Setup

### Linux (NixOS)

```sh
nix develop
python agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder
```

The dev shell provides Python with `prompt-toolkit`, `pygments` and `ruff`. No
venv or `pip` is required. `flake.lock` pins nixpkgs for reproducibility.

### Windows

```powershell
py -m pip install -r requirements.txt
py agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder
```

Use Windows Terminal for the best rendering.

### Prebuilt Windows release

Every `v*` tag publishes a self-contained Windows x64 bundle on the
[Releases page](https://github.com/eskimohunter/BasicAgent/releases). It includes
a stripped CPython 3.14 from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
and `prompt-toolkit`; nothing needs to be installed.

1. Download `BA-<version>-windows-x86_64.zip`.
2. Verify it against the published `.sha256` file (optional but recommended):

   ```powershell
   $hash = (Get-FileHash .\BA-<version>-windows-x86_64.zip -Algorithm SHA256).Hash.ToLower()
   $expected = ((Get-Content .\BA-<version>-windows-x86_64.zip.sha256) -split '\s+')[0]
   $hash -eq $expected
   ```
3. Extract anywhere and run `ba.cmd`, or call the interpreter directly:
   `python\python.exe agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder`

The bundle is not code-signed, so Windows SmartScreen may warn on first launch.
Python itself is distributed under the PSF license (`python\LICENSE.txt`).

## Usage

```sh
# Minimal
python agent.py --base-url http://192.168.1.10:8080/v1 --model qwen2.5-coder

# Via environment variables
export AGENT_BASE_URL=http://192.168.1.10:8080/v1
export AGENT_MODEL=qwen2.5-coder
python agent.py

# Run against a subdirectory
python agent.py --workspace ~/src/myproject
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

### Markdown rendering

Assistant replies are rendered as they stream: headings, bold/italic/strikethrough,
inline and fenced code, lists, task lists, blockquotes, horizontal rules, links
and tables. Fenced code is syntax-highlighted when `pygments` is installed.
Partial markers are held only until they resolve, so prose still appears
token-by-token.

Known limits: emphasis is not nested, fenced code is highlighted line by line,
and tables are printed once the table block ends.

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
 Run?  [ Yes ]  [ No ]
```

- `←` / `→` — highlight Yes or No (Yes is selected by default).
- `Enter` — confirm the highlighted choice.
- `Ctrl+C` / `Ctrl+D` — deny; the model is told the user denied it.

Every command is approved individually; the prompt does not remember earlier
approvals.

The command is executed exactly as shown, via the system shell, in the workspace
directory. There is no hidden wrapping or rewriting.

## Tools

| Tool | Mode | Description |
| --- | --- | --- |
| `read_file(path, start_line?, end_line?)` | both | Numbered lines, max 2000 lines per call; decodes UTF-8/16/32, refuses binary files |
| `list_dir(path?)` | both | Directory listing; directories get a trailing `/` |
| `grep(pattern, path?, include?, ignore_case?, max_results?)` | both | Regex search with line numbers, skips `.git`, `node_modules`, `.venv`, caches, binary files |
| `fetch_url(url, max_bytes?)` | both | HTTP(S) GET, capped at 100 KB by default |
| `write_file(path, content)` | build | Create/overwrite; creates parent directories |
| `edit_file(path, old_string, new_string, replace_all?)` | build | Exact-string replacement; fails on ambiguity |
| `run_command(command, timeout_seconds?)` | build | Shell command; **always requires approval** |

Tool results are capped at 64 KB sent to the model and previewed at 2 KB in the
terminal; the full (capped) result is stored in the audit log. File tools are
confined to the workspace.

## Configuration

Precedence: CLI argument > environment variable > default.

| CLI | Environment | Default | Meaning |
| --- | --- | --- | --- |
| `--base-url` | `AGENT_BASE_URL`, `OPENAI_BASE_URL` | `http://localhost:8080/v1` | API root |
| `--api-key` | `AGENT_API_KEY`, `OPENAI_API_KEY` | `none` | Bearer token |
| `--model` | `AGENT_MODEL`, `OPENAI_MODEL` | `local-model` | Model name |
| `--summary-base-url` | `AGENT_SUMMARY_BASE_URL` | `--base-url` | API root for command summaries |
| `--summary-api-key` | `AGENT_SUMMARY_API_KEY` | `--api-key` | Bearer token for command summaries |
| `--summary-model` | `AGENT_SUMMARY_MODEL` | `--model` | Model for command summaries |
| `--workspace` | — | current directory | Workspace root for tools and shell |
| — | `AGENT_CONTEXT_WINDOW` | probe | Context window in tokens; `0` probes `/props` then `/models` |

Before a shell command is shown in the approval prompt, BA asks the summary
model for a one-line explanation for a junior developer and prints it under
`-> run_command <command>`. Each summary setting falls back to the corresponding
main setting, so no extra configuration is required. Summaries are best-effort:
if the call fails, BA prints the raw command and disables summarisation for the
rest of the session. The command text is sent to the summary endpoint, which
may differ from your main endpoint.

## Audit log

Each session writes
`<app dir>/logs/YYYYMMDD-HHMMSS-<pid>.jsonl` (UTC timestamp) next to `agent.py`,
regardless of the terminal's working directory. One JSON object per line:

| Event | Contents |
| --- | --- |
| `session_start` | version, base URL, model, workspace, mode, redacted API key, project instructions path/size, context window/source |
| `user_message` | user input |
| `assistant_message` | assistant text and any tool calls |
| `command_summary` | command, one-line summary, source (`model` / `cache` / `fallback`) |
| `tool_call` | tool name, full arguments, call id |
| `approval` | command and decision (`allow` / `deny`) |
| `tool_result` | call id, success flag, capped output |
| `mode_change` | new mode |
| `error` | agent or step-limit errors |
| `session_end` | written on exit |

## Security notes

- The API key is never logged; it is recorded as `[redacted]`.
- Shell commands are sent to the summary endpoint (defaults to the main
  endpoint) to generate the one-line explanation.
- Shell commands are displayed verbatim before execution and never rewritten.
- The command allowlist (config-file support planned) is exact-match only; until
  then every command is approved individually.
- File tools are confined to the workspace.
- Tool output sent to the model is truncated to protect the context window; the
  truncation marker is explicit.

## Dependency updates

Dependabot (`.github/dependabot.yml`) checks weekly for updates to:

- **pip** — `requirements.txt` (version and security updates)
- **nix** — `flake.lock` inputs (version updates only; Dependabot does not
  support security updates for the Nix ecosystem)
- **github-actions** — workflow actions

Dependabot *security* updates and alerts for pip/GitHub Actions are enabled in
repository settings (Settings → Code security), not in this file.

## Releasing (Windows)

The `Release (Windows)` workflow
(`.github/workflows/release.yml`) builds a self-contained zip:

1. Bump `VERSION` in `agent.py`, commit, and push.
2. Tag and push:

   ```sh
   git tag v0.2.0
   git push origin v0.2.0
   ```

The workflow then validates that the tag matches `VERSION`, downloads the pinned
python-build-standalone archive, verifies its published SHA-256 checksum, installs
`requirements.txt`, smoke-tests the bundled agent, zips everything with
`ba.cmd`, and publishes a GitHub Release with the zip and a `.sha256`
file. Tags containing a hyphen (e.g. `v0.2.0-rc1`) or a `v0.x` version are marked
as pre-releases.

For a dry run without releasing, trigger the workflow manually (Actions → Release
(Windows) → Run workflow); it uploads the zip as a workflow artifact only.

The bundled interpreter is pinned in the workflow's `env` (`PBS_RELEASE`,
`PYTHON_VERSION`). Dependabot updates the actions, `requirements.txt` and
`flake.lock`, but not these values — bump them deliberately.

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
agent.py                        the entire agent
ba.cmd                          Windows launcher for prebuilt bundles
.gitattributes                  ensures .cmd files use CRLF on checkout
flake.nix                       nix develop environment (Python + prompt-toolkit + ruff)
flake.lock                      pinned nixpkgs
requirements.txt                runtime dependencies for pip (Windows / non-Nix)
ARCHITECTURE.md                 internal design and data flow
.github/dependabot.yml          dependency update configuration
.github/workflows/release.yml   Windows release pipeline
```
