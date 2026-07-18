"""Claude Code hook worker: live blast-radius policing + graph freshness.

Invoked as `python -m codegraph.hook <pre|post> <project_root>` with the hook's
stdin JSON piped in. FAIL-OPEN by construction: any error prints nothing and
exits 0, so a broken graph never disrupts an edit.

  pre  -> PreToolUse: inject the edited file's blast radius via additionalContext
          (the model reads it BEFORE editing). Never blocks.
  post -> PostToolUse: report what relationships the edit changed (systemMessage,
          shown to the user) and fold the edit into the graph (incremental reindex),
          so the next pre-warning and every MCP query stay current.
"""

from __future__ import annotations

import json
import os
import sys


def _edited_path(data: dict) -> str | None:
    ti = data.get("tool_input") or {}
    p = ti.get("file_path") or ti.get("notebook_path")
    # realpath, not abspath: the graph stores realpaths, and a symlink-alias spelling
    # (macOS /tmp -> /private/tmp) would miss the symbol lookup - silently killing
    # the blast-radius warning and reindexing the graph under duplicate alias paths.
    return os.path.realpath(p) if p else None


def _pre(root: str, db: str, fpath: str) -> None:
    from .store import Store
    s = Store(db)
    try:
        syms = s.db.execute(
            "SELECT id,name,container,start_line FROM symbols WHERE file=?", (fpath,)).fetchall()
        if not syms:
            return  # new/unindexed file -> nothing to warn about
        callers: dict[int, int] = {}
        for sym in syms:
            for cid, d in s.reachable(sym["id"], "callers", 3):
                callers[cid] = min(d, callers.get(cid, 1 << 30))
        rel = os.path.relpath(fpath, root)
        if not callers:
            msg = f"codegraph: editing {rel} - {len(syms)} symbol(s), no in-repo callers. Low blast radius."
        else:
            files, items = set(), []
            for cid, _ in sorted(callers.items(), key=lambda kv: kv[1]):
                cs = s.symbol_by_id(cid)
                if not cs:
                    continue
                files.add(cs["file"])
                if len(items) < 12:
                    lbl = f"{cs['container']}.{cs['name']}" if cs["container"] else cs["name"]
                    items.append(f"{lbl} ({os.path.relpath(cs['file'], root)}:{cs['start_line']})")
            msg = (f"codegraph BLAST RADIUS - {rel} has {len(callers)} caller(s) across "
                   f"{len(files)} file(s). Audit these before changing signatures/behavior:\n  - "
                   + "\n  - ".join(items))
            if len(callers) > len(items):
                msg += f"\n  …and {len(callers) - len(items)} more."
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": msg}}))
    finally:
        s.close()


def _post(root: str, db: str, fpath: str) -> None:
    from .store import Store
    from . import graph as G
    from . import indexer as I
    rel = os.path.relpath(fpath, root)
    # Compare RESOLVED, container-qualified out-edges before vs after reindex, so a
    # re-pointed call (Foo.save -> Bar.save, same callee name) is still caught.
    s = Store(db)
    try:
        before = G.resolved_out_edges(s, fpath)
    finally:
        s.close()
    I.index(root, db)  # fold the edit in (incremental; re-resolves)
    s = Store(db)
    try:
        after = G.resolved_out_edges(s, fpath)
    finally:
        s.close()
    added = sorted(f"{a} -> {b}" for a, b in (after - before))
    removed = sorted(f"{a} -> {b}" for a, b in (before - after))
    if added or removed:
        parts = []
        if added:
            parts.append(f"+{len(added)} (" + "; ".join(added[:5]) + ")")
        if removed:
            parts.append(f"-{len(removed)} (" + "; ".join(removed[:5]) + ")")
        print(json.dumps({"systemMessage": f"codegraph: {rel} call edges changed → " + " ".join(parts)}))


def main() -> None:
    try:
        from .indexer import find_graph_root
        mode = sys.argv[1]
        # Walk up to the governing graph (same logic as the MCP server + bash gate),
        # so the worker is correct even if invoked directly with a subdir. realpath
        # first: an alias-spelled cwd must converge on the same graph + stored paths.
        root = find_graph_root(os.path.realpath(sys.argv[2]))
        if not root:
            return
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        fpath = _edited_path(data)
        if not fpath:
            return
        db = os.path.join(root, ".codegraph", "graph.db")
        (_pre if mode == "pre" else _post)(root, db, fpath)
    except Exception:
        return  # FAIL-OPEN: never disrupt the edit


if __name__ == "__main__":
    main()
