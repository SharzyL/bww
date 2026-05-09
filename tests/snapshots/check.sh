#!/usr/bin/env bash
# Diff current dry-run output against captured snapshots.
# Usage: bash tests/snapshots/check.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

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
