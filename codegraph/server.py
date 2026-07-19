"""codegraph MCP server.

One server, every project. Each tool resolves a `root` (default: cwd) and keeps
that project's graph in <root>/.codegraph/graph.db. Read tools build the graph on
first use; after edits, call `index` (or `pending` to see uncommitted drift).
"""

from __future__ import annotations

import json
import os
import sqlite3

from mcp.server.fastmcp import FastMCP

from . import indexer as I
from . import graph as G
from .store import Store

INSTRUCTIONS = """codegraph - an AST-extracted call/symbol graph for auditing blast \
radius BEFORE you edit. The graph is regenerated from source (tree-sitter), so it \
reflects the actual code, not a hand-maintained doc.

WORKFLOW: before changing a function/method's signature or behavior, call `callers` \
(or `blast_radius` for a whole file) to see who depends on it. After editing, the graph \
auto-refreshes; use `pending` to see what call relationships an uncommitted edit changed. \
Run `index` once per project to build the graph (the user can also run /codegraph-reindex).

CONFIDENCE TIERS on every edge (resolution is by call SHAPE + light type inference, not \
full type analysis):
  - exact     = the call shape proves the target (bare call->free function, self->same \
class, typed instance/static->that class). Trustworthy; treat as a real dependency.
  - inferred  = a single plausible target but the receiver's type is unverified. Likely \
but NOT certain - confirm before relying on it.
  - ambiguous = multiple candidates; edges to all (a real caller is never dropped). \
Calls with no in-repo target are 'unresolved' (library/builtin), not faked.
Bias is recall over precision: an uncertain edge is kept and labeled, never dropped.

LIVE POLICING: in projects with a graph, a PreToolUse hook auto-injects a "codegraph \
BLAST RADIUS" message listing a file's callers before you edit it. TREAT THAT AS A \
DIRECTIVE, not noise: audit those callers (especially the exact ones) before changing \
signatures or behavior - they are what breaks. The graph is found by walking UP from your \
cwd to the nearest .codegraph/ (git-style), so a session in a theme/plugin subdir is \
governed by the whole-project graph indexed at the root.

WORDPRESS: call graphs miss WP's string-dispatch coupling (add_action/add_filter/\
do_action/register_rest_route). Use the `hooks` tool: hooks(name=<symbol>) shows which \
hooks a callback fires on; hooks(name=<hookname>) lists listeners; hooks(entry_points=true) \
maps the wp_ajax_*/admin_post_*/REST attack surface with UNAUTH flagged. blast_radius \
also reports a symbol's hook registrations as hook_context.

READING HOOK METRICS (avoid the #1 false "it's broken" conclusion): `edges_hook` counts only SYNTHESIZED fire->listener edges, which exist ONLY when an in-repo do_action/apply_filters meets an in-repo listener. A pure-listener plugin (registers add_action/register_rest_route callbacks that WP CORE or the REST framework fires, never its own do_action) correctly reports `edges_hook: 0` - that is expected, NOT a missing-extractor bug. REST routes are deliberately never joined to fires (the framework fires them), so they never contribute to edges_hook. The captured coupling lives in the HOOKS TABLE, not the edges count: before concluding anything is unextracted, run hooks(entry_points=true) and hooks(name=<a handler>) - the registrations will be right there. edges_hook is the wrong number for "is coupling captured"; the hooks tool is the right check.

SCOPING (do this for the user, proactively): on a big vendor monorepo (WordPress + \
WooCommerce/Dokan, etc.) the graph drowns in third-party code - hotspots bind to vendor \
core re-declarations and almost everything is ambiguous. When `index` returns a \
`scope_suggestion`, ACT ON IT: call `scope(root, exclude=<its suggested_exclude>)` (review \
the list, add any other vendor dirs you recognize) to write .codegraph/config.json and \
re-scope. The user is not expected to configure this - you are. Exclude-based scope only \
drops confirmed vendor, never the user's own code. On a brand-new huge repo, call `scope()` \
with NO args first for an INSTANT dry-run suggestion (no full index needed), then apply it \
- so you never pay for indexing the whole vendor tree just to learn it should be scoped. \
Every `index` result also carries a `scope` field reporting the current scope state."""

mcp = FastMCP("codegraph", instructions=INSTRUCTIONS)


from .indexer import find_graph_root  # shared root resolution (also used by the hook)
from .languages import EXT_LANG       # single source of truth for "is this a file path?"


MAX_DEPTH = 25   # reachability hop ceiling; deeper queries walk the full closure anyway
MAX_TOP = 1000   # hotspots row ceiling


def _clamp(v: int, lo: int, hi: int) -> int:
    """Coerce a tool integer into [lo, hi]; non-ints fall back to lo."""
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return lo


def _like_escape(s: str) -> str:
    r"""Escape LIKE wildcards so a filename fragment is matched literally (used with
    `ESCAPE '\'`). Without this, `%`/`_` in a target silently widen the match."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _guard_root(r: str) -> None:
    """Refuse pathological roots - the filesystem root, the home directory, or any
    ancestor of home. Indexing those would walk an enormous tree, build a giant DB,
    and scatter `.codegraph/` dirs. The caller already has full file access; this is a
    footgun/DoS guard, not a privilege boundary. Point codegraph at a project dir."""
    home = os.path.realpath(os.path.expanduser("~"))
    rr = os.path.realpath(r)
    try:
        too_broad = os.path.commonpath([rr, home]) == rr  # rr == home or an ancestor of it
    except ValueError:
        too_broad = False  # different volume from home -> not an ancestor
    if not too_broad and os.path.isdir(rr):
        # Inode-level check: APFS is case-insensitive, so a case-variant spelling
        # (/USERS/x) or a firmlink alias defeats the string comparison above while
        # still opening the same enormous tree. Walk home's ancestry by identity.
        try:
            rid = os.stat(rr)
            d = home
            while True:
                st = os.stat(d)
                if (st.st_dev, st.st_ino) == (rid.st_dev, rid.st_ino):
                    too_broad = True
                    break
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        except OSError:
            pass  # unstatable -> fall through to the string verdict
    if rr == os.path.sep or too_broad:
        raise ValueError(
            f"refusing to operate on {r!r}: it is the filesystem root, your home "
            f"directory, or an ancestor of it. Point codegraph at a specific project "
            f"directory instead.")


def _root(root: str) -> str:
    """Resolve the governing project root. If the given path (default cwd) has no
    graph, walk up to the one that does - so a session launched in a plugin/theme
    subdir uses the whole-site graph indexed at the WP root. Falls back to the path
    itself when no ancestor graph exists (so `index` there creates a new one)."""
    r = os.path.realpath(os.path.expanduser(root or "."))
    _guard_root(r)
    return find_graph_root(r) or r


def _db(root: str) -> str:
    return os.path.join(_root(root), ".codegraph", "graph.db")


def _ensure(root: str) -> str:
    """Build the graph if this project has none yet; return db path. A corrupt DB
    (truncated mid-write) is quarantined and rebuilt - the graph is a derived cache,
    so healing it beats every tool raising sqlite3.DatabaseError forever."""
    db = _db(root)
    if not os.path.exists(db):
        I.index(_root(root), db)
        return db
    try:
        s = Store(db)
        try:
            s.db.execute("SELECT 1 FROM files LIMIT 1").fetchone()
        finally:
            s.close()
    except sqlite3.DatabaseError:
        I.index(_root(root), db)  # index() quarantines the corrupt DB and rebuilds
    return db


def _label(s) -> str:
    return f"{s['container']}.{s['name']}" if s["container"] else s["name"]


def _ref(s) -> dict:
    return {"symbol": _label(s), "kind": s["kind"], "file": s["file"],
            "line": s["start_line"], "lang": s["lang"]}


def _resolve(store: Store, name: str, lang=None, kind=None, file=None):
    """Map a symbol name to definition rows, optionally narrowed. '.'-qualified
    names (Class.method) narrow by container."""
    container = None
    if "." in name and "::" not in name:
        container, _, bare = name.rpartition(".")
        name = bare
    rows = store.find(name, lang=lang, kind=kind)
    if container:
        rows = [r for r in rows if (r["container"] or "") == container]
    if file:
        rows = [r for r in rows if file in r["file"]]
    return rows


@mcp.tool()
def index(root: str = ".", force: bool = False) -> dict:
    """Build or incrementally update the code graph for a project.

    Parses every supported source file (Python/JS/TS/PHP/Ruby/Go) under `root`,
    honouring .gitignore. Incremental by content hash - unchanged files are skipped.
    Run this after edits to refresh blast-radius queries. `force=True` reparses all.
    On a large unscoped repo the result includes a `scope_suggestion` - act on it via `scope`.
    """
    return I.index_safe(_root(root), _db(root), force=force)


@mcp.tool()
def scope(root: str = ".", include: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    """Scope which files codegraph indexes for this project, then force-reindex. On a big
    vendor monorepo this is the highest-leverage action - and it's YOURS to do for the user,
    not theirs. Pass `exclude` to drop vendor trees (recommended: keeps ALL the user's code,
    auto-includes anything they add later) or `include` to index ONLY listed dirs. Globs are
    relative to the project root; `*` spans directories (e.g. "wp-content/plugins/woocommerce/*").
    Merges with any existing config and writes <root>/.codegraph/config.json. Apply an index()
    `scope_suggestion` straight through here. Returns the new config + post-scope stats.

    Call with NO include/exclude for an instant DRY RUN: a suggested exclude list derived from
    the directory layout (no indexing) - so you can scope a huge repo BEFORE paying for a full
    index, then apply it. Returns {"dry_run": True, "suggested_exclude": [...], "current": ...}."""
    r = _root(root)
    if isinstance(include, str):  # a bare string would iterate char-by-char into '*' globs
        include = [include]
    if isinstance(exclude, str):
        exclude = [exclude]
    if include is None and exclude is None:  # dry run - suggest, don't write or reindex
        cur = {}
        cpath = os.path.join(r, ".codegraph", "config.json")
        if os.path.exists(cpath):
            try:
                cur = json.load(open(cpath))
            except (OSError, ValueError):
                cur = {}
        return {"dry_run": True, "current_config": cur if isinstance(cur, dict) else {},
                **(I.suggest_scope(r) or {"suggested_exclude": [], "reason": "no vendor dirs auto-detected"})}
    os.makedirs(os.path.join(r, ".codegraph"), exist_ok=True)
    path = os.path.join(r, ".codegraph", "config.json")
    cfg = {}
    if os.path.exists(path):
        try:
            loaded = json.load(open(path))
            cfg = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            cfg = {}

    def _merge(key, new):
        if new:
            cur = [g for g in (cfg.get(key) or []) if isinstance(g, str)]
            cfg[key] = sorted({*cur, *(g for g in new if isinstance(g, str) and g)})

    _merge("include", include)
    _merge("exclude", exclude)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    res = I.index_safe(r, _db(root), force=True)
    keep = ("discovered", "edges_exact", "edges_inferred", "edges_ambiguous", "edge_rows", "unresolved")
    return {"config": cfg, "config_path": path, "reindexed": {k: res.get(k) for k in keep}}


@mcp.tool()
def stats(root: str = ".") -> dict:
    """Graph overview: file/symbol/edge counts, resolution quality (exact vs
    inferred vs ambiguous vs unresolved), and breakdown by language and symbol kind."""
    store = Store(_ensure(root))
    try:
        return store.stats()
    finally:
        store.close()


@mcp.tool()
def find(root: str = ".", name: str = "", lang: str | None = None, kind: str | None = None) -> dict:
    """Locate where a symbol is DEFINED. Returns every matching definition with
    file:line and signature. Use before callers/callees when a name is ambiguous."""
    store = Store(_ensure(root))
    try:
        rows = _resolve(store, name, lang=lang, kind=kind)
        return {"matches": [{**_ref(r), "signature": r["signature"]} for r in rows]}
    finally:
        store.close()


@mcp.tool()
def callers(root: str = ".", symbol: str = "", depth: int = 3, lang: str | None = None) -> dict:
    """Reverse reachability - everything that (transitively) calls `symbol`. This is
    the blast radius of changing it: who breaks if its behaviour or signature moves.
    `depth` bounds the hops. Each node carries a resolution tag: `exact` (proven by
    call shape - a certain dependency), `inferred` (single plausible target, receiver
    type unverified), or `ambiguous` (multiple candidates). Audit the exact callers first."""
    return _reach(root, symbol, depth, lang, "callers")


@mcp.tool()
def callees(root: str = ".", symbol: str = "", depth: int = 3, lang: str | None = None) -> dict:
    """Forward reachability - everything `symbol` (transitively) calls. What it
    depends on; what you'd have to mock to test it in isolation."""
    return _reach(root, symbol, depth, lang, "callees")


def _reach(root, symbol, depth, lang, direction):
    depth = _clamp(depth, 1, MAX_DEPTH)
    store = Store(_ensure(root))
    try:
        roots = _resolve(store, symbol, lang=lang)
        if not roots:
            return {"error": f"no definition named '{symbol}'", "hint": "try find()"}

        def reach_nodes(seed_id):
            seen: dict[int, int] = {}
            for sid, d in store.reachable(seed_id, direction, depth):
                seen[sid] = min(d, seen.get(sid, 1 << 30))
            out = []
            for sid, d in sorted(seen.items(), key=lambda kv: kv[1]):
                s = store.symbol_by_id(sid)
                if s:
                    out.append({**_ref(s), "depth": d})
            return out

        if len(roots) == 1:
            nodes = reach_nodes(roots[0]["id"])
            return {
                "symbol": symbol,
                "matched_definitions": [_ref(roots[0])],
                "direction": direction,
                "count": len(nodes),
                "max_depth": depth,
                "nodes": nodes,
            }
        # Ambiguous bare name -> group per seed definition. Blending the callers of
        # Config.save with those of User.save makes the LLM hallucinate a cross-class
        # refactor and blow out its context. Keep them separate and tell it to qualify.
        bare = symbol.rpartition(".")[2] or symbol
        groups = []
        for r in roots:
            ns = reach_nodes(r["id"])
            groups.append({"definition": _ref(r), "count": len(ns), "nodes": ns})
        return {
            "symbol": symbol,
            "ambiguous": True,
            "direction": direction,
            "matched_definitions": [_ref(r) for r in roots],
            "note": (f"'{symbol}' matches {len(roots)} definitions; results are grouped per "
                     f"definition. Re-query with a container-qualified name (e.g. "
                     f"'<Container>.{bare}') to target exactly one."),
            "max_depth": depth,
            "groups": groups,
        }
    finally:
        store.close()


@mcp.tool()
def neighbors(root: str = ".", symbol: str = "", lang: str | None = None) -> dict:
    """Immediate in/out edges of a symbol: its direct callers and direct callees,
    each with the call-site line and resolution tag. One hop, full detail."""
    store = Store(_ensure(root))
    try:
        rows = _resolve(store, symbol, lang=lang)
        if not rows:
            return {"error": f"no definition named '{symbol}'", "hint": "try find()"}
        out = []
        for r in rows:
            inc, outg = store.neighbors(r["id"])
            out.append({
                "definition": _ref(r),
                "called_by": [{**_ref(x), "at_line": x["call_line"], "resolution": x["resolution"]} for x in inc],
                "calls": [{**_ref(x), "at_line": x["call_line"], "resolution": x["resolution"]} for x in outg],
            })
        return {"symbol": symbol, "definitions": out}
    finally:
        store.close()


@mcp.tool()
def blast_radius(root: str = ".", target: str = "", depth: int = 6) -> dict:
    """Full impact set of touching `target` - a symbol name OR a file path. For a
    file, unions the blast radius of every symbol defined in it. Returns the
    affected symbols grouped by file: the audit surface before an edit."""
    if not target.strip():
        return {"error": "target required: a symbol name or file path"}
    target = target.strip()
    while target.startswith("./"):  # './app.py' must match like 'app.py'
        target = target[2:]
    depth = _clamp(depth, 1, MAX_DEPTH)
    store = Store(_ensure(root))
    try:
        # Resolve as a SYMBOL first (consistent with callers/callees), so a qualified
        # name whose method equals a language token - Runner.go, X.rb - isn't misrouted
        # to a file search. Only fall to a file audit on a symbol miss, and only when the
        # target looks like a path (has a separator or a known source extension).
        seeds = [] if os.sep in target else _resolve(store, target)
        is_file_target = not seeds and (os.sep in target
                                        or os.path.splitext(target)[1].lower() in EXT_LANG)
        if is_file_target:
            # Anchor on a path-segment boundary so `Task.go` can't match `Task.go_dir/x.py`.
            seeds = store.db.execute(
                "SELECT * FROM symbols WHERE file = ? OR file LIKE ? ESCAPE '\\'",
                (target, f"%/{_like_escape(target)}")).fetchall()
        if not seeds:
            return {"error": f"no symbol or file matching '{target}'"}
        impacted: dict[int, int] = {}
        for s in seeds:
            for sid, d in store.reachable(s["id"], "callers", depth):
                impacted[sid] = min(d, impacted.get(sid, 1 << 30))
        by_file: dict[str, list] = {}
        for sid, d in impacted.items():
            s = store.symbol_by_id(sid)
            if s:
                by_file.setdefault(s["file"], []).append({"symbol": _label(s), "line": s["start_line"], "depth": d})
        for v in by_file.values():
            v.sort(key=lambda x: x["line"])
        # WordPress hook context: is any seed itself a hook handler / entry point?
        hook_ctx = []
        for s in seeds:
            for h in store.hooks_for_symbol(s["id"]):
                tag = "UNAUTH ENTRY" if h["unauth"] else ("ENTRY" if h["entry_point"] else h["hook_class"])
                hook_ctx.append(f"{_label(s)} fires on '{h['hook']}' [{tag}]"
                                + (f" - {h['note']}" if h["note"] else ""))
        out = {
            "target": target,
            "seed_symbols": [_ref(s) for s in seeds],
            "impacted_symbol_count": len(impacted),
            "impacted_file_count": len(by_file),
            "by_file": dict(sorted(by_file.items())),
        }
        # A bare symbol that matches several definitions blends their blast radii.
        # Disclose it so the LLM qualifies instead of trusting a merged set. (A file
        # target legitimately unions every symbol it defines - not flagged.)
        if not is_file_target and len(seeds) > 1:
            out["ambiguous_seed"] = True
            out["note"] = (f"'{target}' matches {len(seeds)} definitions; the blast radius "
                           f"below is their UNION. Re-query with a container-qualified name "
                           f"to isolate one.")
        elif is_file_target:
            seed_files = sorted({s["file"] for s in seeds})
            if len(seed_files) > 1:  # basename matched several files -> disclose the union
                out["ambiguous_seed"] = True
                out["note"] = (f"'{target}' matches {len(seed_files)} files; the blast radius "
                               f"is their UNION. Pass a longer path suffix to isolate one: "
                               + "; ".join(seed_files[:8]))
        if hook_ctx:
            out["hook_context"] = hook_ctx
        return out
    finally:
        store.close()


@mcp.tool()
def path(root: str = ".", src: str = "", dst: str = "") -> dict:
    """Shortest call path from `src` to `dst` - how one symbol reaches another
    through the call graph. Empty if no path exists."""
    store = Store(_ensure(root))
    try:
        a = _resolve(store, src)
        b = _resolve(store, dst)
        if not a or not b:
            return {"error": "src or dst not found", "src_found": bool(a), "dst_found": bool(b)}
        dst_ids = {r["id"] for r in b}
        # src/dst may each resolve to several same-named defs. Take the globally
        # shortest path over ALL pairs, not the first pair that happens to connect -
        # else a 10-hop A.save->X.load can hide a 1-hop B.save->X.load.
        best = None
        adj = G.build_adj(store)  # one edge scan shared by every candidate pair
        for s in a:
            for t in dst_ids:
                p = G.shortest_path(store, s["id"], t, adj)
                if p and (best is None or len(p) < len(best)):
                    best = p
            if best is not None and len(best) <= 2:  # 1 hop (or 0) - unbeatable
                break
        if best is None:
            return {"hops": None, "path": [], "note": "no path"}
        return {"hops": len(best) - 1,
                "path": [_label(store.symbol_by_id(i)) for i in best],
                "detail": [_ref(store.symbol_by_id(i)) for i in best],
                "src_candidates": len(a), "dst_candidates": len(dst_ids)}
    finally:
        store.close()


@mcp.tool()
def cycles(root: str = ".") -> dict:
    """Circular dependencies in the call graph: strongly-connected components
    (mutual recursion / dependency loops) and self-recursive functions."""
    store = Store(_ensure(root))
    try:
        comps = G.cycles(store, min_size=2)
        out = []
        for comp in sorted(comps, key=len, reverse=True):
            members = [store.symbol_by_id(i) for i in comp]
            out.append({"size": len(comp),
                        "members": [{"symbol": _label(m), "file": m["file"], "line": m["start_line"]}
                                    for m in members if m]})
        return {"cycle_count": len(out), "cycles": out}
    finally:
        store.close()


@mcp.tool()
def hotspots(root: str = ".", top: int = 20) -> dict:
    """Load-bearing symbols ranked by fan-in (how many things depend on them) - where a
    careless edit hurts most. Betweenness centrality (how often a node sits on dependency
    paths) is ALSO returned, but only for graphs up to ~1500 nodes (it's all-pairs and too
    costly above that); past that, `betweenness_computed` is false and only fan-in ranks.
    Tip: on a large vendor monorepo, scope the graph to your own code (.codegraph/config.json
    `include`) so fan-in reflects your app, not a re-declared core function in a vendor file."""
    top = _clamp(top, 1, MAX_TOP)
    store = Store(_ensure(root))
    try:
        return G.hotspots(store, top=top)
    finally:
        store.close()


@mcp.tool()
def pending(root: str = ".") -> dict:
    """Relationship drift from UNCOMMITTED edits. For every file changed on disk
    since the last index, diffs its RESOLVED call edges (caller -> resolved target,
    container-qualified) against the stored graph and reports what each edit ADDED or
    REMOVED - catching a re-pointed call (Foo.save -> Bar.save) a name diff would miss.
    Run after an edit to prove you didn't silently sever a relationship. Folds the
    changes into the graph as part of the diff."""
    db = _ensure(root)
    store = Store(db)
    try:
        return G.pending(store, _root(root), db)
    finally:
        store.close()


@mcp.tool()
def hooks(root: str = ".", name: str = "", entry_points: bool = False) -> dict:
    """WordPress hook/filter dispatch - the string-named action/filter coupling a call
    graph CANNOT see (add_action/add_filter/register_rest_route/do_action/apply_filters).

    - `name` = a hook/action/filter name or REST route -> its in-repo listeners + fire sites.
    - `name` = a symbol -> the hooks it's registered on (the high-confidence callback->hook
      direction: "register_routes fires on rest_api_init").
    - `entry_points=True` or empty `name` -> the public attack surface: every wp_ajax_* /
      wp_ajax_nopriv_* / register_rest_route callback, with UNAUTH flagged.

    Blind spots (honest): dynamic/interpolated hook names, closures, and variable
    callbacks (call_user_func($x)) cannot be resolved and are not edges.

    This tool - NOT the `edges_hook` count - is the source of truth for whether
    WordPress coupling was captured. `edges_hook: 0` is normal for a pure-listener
    plugin (its callbacks are fired by WP core / the REST framework, not by its own
    do_action), and REST routes never contribute to that count by design. If a hook
    seems missing, check here first."""
    store = Store(_ensure(root))
    try:
        if entry_points or not name:
            eps = store.entry_points()
            return {"entry_points": [{
                "auth": "UNAUTH" if e["unauth"] else "auth",
                "type": e["hook_class"], "hook": e["hook"],
                "callback": (f"{e['cb_sym_cont']}.{e['cb_sym_name']}" if e["cb_sym_cont"]
                             else e["cb_sym_name"]) or e["cb_raw"],
                "where": f"{os.path.relpath(e['cb_file'], _root(root))}:{e['cb_line']}" if e["cb_file"] else None,
                "note": e["note"] or None,
            } for e in eps], "count": len(eps)}

        # hook name?
        listeners = store.listeners_of_hook(name)
        fires = store.fires_of_hook(name)
        if listeners or fires:
            return {
                "hook": name,
                "listeners": [{
                    "callback": (f"{r['cb_sym_cont']}.{r['cb_sym_name']}" if r["cb_sym_cont"]
                                 else r["cb_sym_name"]) or r["cb_raw"],
                    "type": r["hook_class"], "resolved": r["callback_symbol"] is not None,
                    "at": f"{os.path.relpath(r['file'], _root(root))}:{r['line']}",
                } for r in listeners],
                "fired_at": [f"{os.path.relpath(f['file'], _root(root))}:{f['line']}" for f in fires],
            }

        # symbol? -> callback->hook direction
        syms = _resolve(store, name)
        regs = []
        for s in syms:
            for h in store.hooks_for_symbol(s["id"]):
                regs.append({"symbol": _label(s), "fires_on": h["hook"], "type": h["hook_class"],
                             "entry_point": bool(h["entry_point"]), "unauth": bool(h["unauth"]),
                             "note": h["note"] or None})
        if regs:
            return {"symbol": name, "registered_on": regs}
        return {"error": f"no hook, route, or registered callback matching '{name}'",
                "hint": "try entry_points=true for the attack surface, or stats() for hook counts"}
    finally:
        store.close()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
