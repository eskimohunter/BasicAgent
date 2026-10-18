# AGENTS.md

## Repo overview

- `agent.py` (~2.9k lines) is the entire product: a single-file, auditable LLM
  coding agent (stdlib + prompt_toolkit TUI, OpenAI-compatible backend).
  Keeping it one file is a stated design goal — do not split it into modules.
- `ARCHITECTURE.md` is the design doc, but its `agent.py:NNN` line references
  are stale; navigate by the section banners in the file instead.
- Python 3.10+; the dev shell uses 3.14 and the Windows bundle 3.14.8.
- `ToDo.md` tracks pending work; check it before picking up tasks.

## Commands

- Dev shell: `nix develop` (Python + prompt-toolkit + pygments + ruff +
  OpenCodeReview's `ocr` CLI).
  Non-Nix equivalent: `pip install -r requirements.txt`.
- There is no test suite. Verification is
  `nix develop -c ruff check .` and `nix develop -c python agent.py --version`.
- Ruff gotcha: the flake-pinned ruff enforces more than the historical default
  rule set (e.g. `DTZ005` naive datetimes, `I001` import order). Keep it clean
  by fixing code; suppression is a last resort.
- Exercising the TUI without a model — start the scripted mock (inside
  `nix develop`) and point BA at it:

  ```sh
  kill $(cat /tmp/ba-mock.pid) 2>/dev/null; rm -rf /tmp/demo-project /tmp/ba-mock.pid
  python demo/mock_llm.py --scenario plan --port 8899 > /tmp/ba-mock.log 2>&1 & echo $! > /tmp/ba-mock.pid
  cp -r demo/workspace /tmp/demo-project
  AGENT_BASE_URL=http://127.0.0.1:8899/v1 \
  AGENT_SUMMARY_BASE_URL=http://127.0.0.1:8899/v1 \
    python agent.py --model demo-model --workspace /tmp/demo-project
  # stop the mock with: kill $(cat /tmp/ba-mock.pid)
  ```

  Scenarios: `plan`, `build`, `boundary`. The first line clears a stale mock
  and fixture copy from a previous run.

- Demo GIFs (local only): `nix develop .#demo -c demo/generate.sh` renders all
  four tapes (~7 min). A single tape works too, but `AGENT_BASE_URL` and
  `AGENT_SUMMARY_BASE_URL` must be exported as `demo/generate.sh` does.
- Workflow edits: `nix run nixpkgs#actionlint -- .github/workflows/<file>.yml`.

## Invariants

- New constants go in the grouped `Constants` section at the top of `agent.py`,
  not inline where they are used.
- Every file-tool path must go through `resolve_path` (workspace confinement).
  The shell (`run_command`) is deliberately unconfined, gated by approval.
- `VERSION` in `agent.py` is the single source of truth; release tags must
  match it (`release.yml` verifies).
- Config precedence: CLI flag > env var > `.env` in cwd > `.env` next to
  `agent.py`. Audit logs go to `<app dir>/logs/` (gitignored), never the
  workspace.
- BA loads a workspace-root `Agents.md` (any capitalization, including this
  file) into its system prompt, and renders `Instructions.md` from the app dir
  at startup (gitignored).
- The README documents exact tool behavior, slash commands and UI text; keep it
  in sync when behavior changes.

## CI and git

- `ci.yml` runs on every PR and master push: pip smoke test, flake-pinned
  `ruff check .`, dev-shell smoke test. `check` is a required status on master.
- `dependabot-automerge.yml` approves and auto-merges Dependabot PRs after CI
  passes.
- `gifs.yml` renders GIFs and uploads them as artifacts — it does **not**
  commit. Commit regenerated `media/*.gif` by hand.
- Direct pushes to master are rejected by the "Master" ruleset (required
  `check` status). Leave commits local; the maintainer pushes.
- `release.yml` builds the Windows zip on `v*` tags; `PYTHON_VERSION` and
  `PBS_RELEASE` are pinned deliberately — bump only when asked.
- `media/**` is Git LFS.

## Demo/VHS quirks (hard-won)

- Inline every `Set`/`Output` in each tape: VHS drops `Output` from `Source`d
  tapes.
- Never use `Wait` in tapes — it stalls rendering with the pinned VHS/Chromium;
  use `Sleep` tuned to the mock's paced output.
- The `demo` devShell must include `jetbrains-mono` or terminal metrics (and
  therefore wrap points) are wrong.
- Recordings run in `/tmp` copies with a hidden prologue; the app is launched
  hidden so GIFs open on the loaded UI. Keep that structure.

## Style

- `agent.py` is heavily documented (module and function docstrings, section
  banners); match that density. Comments explain why, not what.
