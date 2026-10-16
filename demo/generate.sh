#!/usr/bin/env bash
# Render every VHS tape in demo/tape/ to media/*.gif.
#
# Run inside the demo shell:  nix develop .#demo -c demo/generate.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v vhs >/dev/null 2>&1; then
  echo "vhs not found; run: nix develop .#demo -c demo/generate.sh" >&2
  exit 1
fi

# agent.py reads environment variables before any local .env file, so the
# tapes are hermetic even on a machine with a real BA configuration.
export AGENT_BASE_URL="http://127.0.0.1:8899/v1"
export AGENT_SUMMARY_BASE_URL="http://127.0.0.1:8899/v1"

for tape in demo/tape/*.tape; do
  echo "==> rendering ${tape}"
  vhs "${tape}"
done

ls -lh media/demo-*.gif
