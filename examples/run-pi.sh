#!/bin/bash
# Launch the real `pi` coding agent inside a gitx session.
#
# Usage:
#   bash examples/run-pi.sh [repo-dir] -- [pi-args...]
#   bash examples/run-pi.sh ~/code/myproj                     # interactive TUI
#   bash examples/run-pi.sh ~/code/myproj -- -p "fix the test"  # headless
#
# That's it — the agent runs in its normal environment (HOME, PATH, auth,
# config all your own); gitx just makes sure the session worktree carries
# the locks from .gitx.toml before exec'ing.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
AG="python3 -m gitx"

REPO="${1:-$PWD}"
shift || true
PI_ARGS=()
if [ "${1:-}" = "--" ]; then
    shift
    PI_ARGS=("$@")
fi

command -v pi >/dev/null || { echo "pi not found on PATH" >&2; exit 1; }

cd "$REPO"
$AG init >/dev/null 2>&1 || true
SID=$($AG session list | tail -1 | awk '{print $1}')
if [ -z "$SID" ] || ! $AG session show "$SID" >/dev/null 2>&1; then
    SID=$($AG session create | head -1 | awk '{print $2}')
fi

if [ ${#PI_ARGS[@]} -gt 0 ]; then
    exec $AG run "$SID" -- pi "${PI_ARGS[@]}"
else
    exec $AG run "$SID" -- pi
fi
