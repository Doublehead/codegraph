#!/usr/bin/env bash
# Remove codegraph from this machine: unregister the MCP server + hooks (restoring the
# pre-install backups if present), delete the hooks/command, and optionally the engine.
# Does NOT touch any project's .codegraph/ graph dirs.
set -euo pipefail

ENGINE="$HOME/.local/lib/codegraph"
CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

echo "Unregistering codegraph from Claude Code config..."
python3 - <<'PY'
import json, os
HOME = os.path.expanduser("~")
_CFG = os.environ.get("CLAUDE_CONFIG_DIR")
CLAUDE_JSON = os.path.join(_CFG, ".claude.json") if _CFG else os.path.join(HOME, ".claude.json")
SETTINGS = os.path.join(_CFG or os.path.join(HOME, ".claude"), "settings.json")
def edit(path, fn):
    if not os.path.exists(path): return
    with open(path) as f: data = json.load(f)
    if fn(data):
        with open(path, "w") as f: json.dump(data, f, indent=2); f.write("\n")
        print(f"  cleaned {path}")

def drop_mcp(d):
    s = d.get("mcpServers", {})
    return s.pop("codegraph", None) is not None

def drop_hooks(d):
    # Detect removal PER GROUP BEFORE mutating: the old in-place filter made the
    # post-mutation comparison always equal, so codegraph hooks sharing a group with
    # user hooks were filtered in memory but never written out.
    hooks = d.get("hooks", {}); changed = False
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            kept = [h for h in g.get("hooks", []) if "codegraph" not in h.get("command", "")]
            if len(kept) != len(g.get("hooks", [])):
                changed = True
            g["hooks"] = kept
            if kept:
                groups.append(g)
        hooks[ev] = groups
    return changed

edit(CLAUDE_JSON, drop_mcp)
edit(SETTINGS, drop_hooks)
PY

rm -f "$CLAUDE/hooks/codegraph-police.sh" "$CLAUDE/hooks/codegraph-watch.sh" "$CLAUDE/commands/codegraph-reindex.md"
echo "  removed hooks + command"

read -r -p "Also delete the engine at $ENGINE ? [y/N] " yn
if [ "${yn:-N}" = "y" ] || [ "${yn:-N}" = "Y" ]; then
  rm -rf "$ENGINE"; echo "  removed $ENGINE"
fi
echo "Done. Restart Claude Code. (Project .codegraph/ graph dirs were left untouched — delete them per-project if you want.)"
