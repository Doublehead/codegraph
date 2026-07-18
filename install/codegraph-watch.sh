#!/bin/bash
# SessionStart: ensure a background watcher is running for this project's graph.
# NON-BLOCKING: it only spawns the detached watcher and returns immediately, so session
# start is never stalled (on a big repo a synchronous scan added 20-30s). The watcher does
# an incremental refresh on its first poll — in the background — and then keeps the graph
# live against IDE/git changes. Walks UP to the governing graph; FAIL-OPEN; no stdout.

find_root() {
  # $HOME check FIRST: a stray ~/.codegraph/graph.db must never govern (it would
  # silently capture every graphless project) — mirrors indexer.find_graph_root.
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    [ "$d" = "$HOME" ] && return 1
    [ -f "$d/.codegraph/graph.db" ] && { printf '%s' "$d"; return 0; }
    d=$(dirname "$d")
  done
  return 1
}

root=$(find_root "${CLAUDE_PROJECT_DIR:-$PWD}") || exit 0
py="$HOME/.local/lib/codegraph/.venv/bin/python"
[ -x "$py" ] || exit 0
# launch detached watcher (it self-dedups, refreshes on first poll in the background)
nohup "$py" -m codegraph.watch "$root" >/dev/null 2>&1 &
disown 2>/dev/null
exit 0
