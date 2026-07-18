#!/bin/bash
# codegraph live-policing hook gate.  Arg $1 = pre | post.
# FAIL-OPEN by construction: every miss exits 0 so an edit is never disrupted.
# Walks UP from the launch dir (git-style) to the nearest project holding a
# codegraph graph — so a session started in a theme/plugin subdir is policed by
# the whole-site graph indexed at the WP root. No bash `timeout` (macOS lacks it);
# the harness `timeout` in settings.json bounds the worker instead.

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
exec "$py" -m codegraph.hook "$1" "$root"
