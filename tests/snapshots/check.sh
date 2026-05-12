#!/usr/bin/env bash
# Diff current dry-run output against captured snapshots.
# Usage: bash tests/snapshots/check.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# The Nix dev-shell leaks `kdl-py 1.2.0` into PYTHONPATH via propagated
# build inputs, which would shadow the venv's git-pinned v2 build (the
# loader needs `Node.entries`, v2-only). Strip it so `uv run` picks up
# the venv's site-packages cleanly.
unset PYTHONPATH

fail=0
for prof in dev defaults.firefox defaults.chromium; do
  fname="tests/snapshots/$(echo "$prof" | tr . _).txt"
  current=$(uv run bww --config example/config.kdl -p "$prof" --dry-run echo hi 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
  expected=$(cat "$fname")
  if [[ "$current" != "$expected" ]]; then
    echo "=== DIFF for $prof ==="
    diff <(echo "$expected") <(echo "$current") || true
    fail=1
  fi
done

if [[ $fail -eq 0 ]]; then
  echo "All 3 snapshots match."
else
  echo "Snapshot drift detected — review diffs above."
  exit 1
fi
