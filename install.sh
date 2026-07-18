#!/usr/bin/env bash
# codegraph installer — sets up the engine, MCP server, and live-policing hooks on a Mac.
#
# Idempotent and re-runnable. Backs up any config it edits. Single-user (no dual-account).
# Run from the source directory you copied/cloned onto the target machine:
#
#     ./install.sh
#
# It installs the engine to ~/.local/lib/codegraph, creates its venv, verifies with the
# test suite, copies the hooks/command into ~/.claude, and registers the MCP server +
# hooks into Claude Code's config (with backups). Restart Claude Code afterward.
set -euo pipefail

ENGINE="$HOME/.local/lib/codegraph"
CLAUDE="$HOME/.claude"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "codegraph installer"
echo "  source: $SRC"
echo "  engine: $ENGINE"
echo

# 1. Prerequisites ----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found. Install Python 3.10+ first."; exit 1; }
python3 - <<'PY' || { echo "ERROR: Python 3.10+ required."; exit 1; }
import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PY
HAVE_CLAUDE=1
command -v claude >/dev/null 2>&1 || { HAVE_CLAUDE=0; echo "WARN: 'claude' CLI not on PATH — that's fine, registration is done via config files, not the CLI."; }

# 2. Copy the engine package to ~/.local/lib/codegraph (skip if installing in place) -----
mkdir -p "$ENGINE"
if [ "$SRC" != "$ENGINE" ]; then
  echo "Copying engine package -> $ENGINE"
  rsync -a --delete \
    --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '.codegraph' \
    "$SRC/codegraph" "$SRC/tests" "$SRC/pyproject.toml" "$SRC/README.md" "$ENGINE/" 2>/dev/null \
    || cp -R "$SRC/codegraph" "$SRC/tests" "$SRC/pyproject.toml" "$SRC/README.md" "$ENGINE/"
fi

# 3. venv + pinned dependencies ---------------------------------------------
echo "Creating venv + installing pinned dependencies..."
python3 -m venv "$ENGINE/.venv"
"$ENGINE/.venv/bin/pip" install -q --upgrade pip
"$ENGINE/.venv/bin/pip" install -q -e "$ENGINE"

# 4. Verify the install with the regression suite ---------------------------
echo "Verifying install (regression suite)..."
"$ENGINE/.venv/bin/python" "$ENGINE/tests/test_codegraph.py" | tail -1

# 5. Hooks + slash command --------------------------------------------------
echo "Installing hooks + /codegraph-reindex command..."
mkdir -p "$CLAUDE/hooks" "$CLAUDE/commands"
cp "$SRC/install/codegraph-police.sh" "$SRC/install/codegraph-watch.sh" "$CLAUDE/hooks/"
chmod +x "$CLAUDE/hooks/codegraph-police.sh" "$CLAUDE/hooks/codegraph-watch.sh"
cp "$SRC/install/codegraph-reindex.md" "$CLAUDE/commands/"

# 6. Register MCP server + hooks into Claude Code config (idempotent, backed up) ---------
echo "Registering with Claude Code..."
"$ENGINE/.venv/bin/python" "$SRC/install/merge_config.py"

echo
echo "codegraph installed."
echo "  Next: restart Claude Code, then run /codegraph-reindex inside a project to build its graph."
echo "  Verify the server:  claude mcp get codegraph   (should say Connected)"
