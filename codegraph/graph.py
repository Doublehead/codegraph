"""Graph algorithms over the stored call graph: cycles (SCC), centrality,
shortest dependency path, and the pending edge-delta diff.

All operate on the symbol-level directed graph (src calls dst).
"""

from __future__ import annotations

import os
from collections import defaultdict, deque

from . import parser as P
from . import indexer as I
from . import languages as L
from .store import Store



def sccs(edges: list[tuple[int, int]]) -> list[list[int]]:
    adj: dict[int, list[int]] = defaultdict(list)
    nodes = set()
    for s, d in edges:
        adj[s].append(d)
        nodes.add(s); nodes.add(d)

    index = {}
    low = {}
    on_stack = set()
    stack = []
    result = []
    counter = [0]

    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter[0]
                counter[0] += 1
                stack.append(node); on_stack.add(node)
            recurse = False
            children = adj[node]
            i = pi
            while i < len(children):
                w = children[i]
                if w not in index:
                    work.append((w, 0))
                    work[-2] = (node, i + 1)
                    recurse = True
                    break
                elif w in on_stack:
                    low[node] = min(low[node], index[w])
                i += 1
            if recurse:
                continue
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


def cycles(store: Store, min_size: int = 2) -> list[list[int]]:
    """SCCs with >1 member (mutual recursion / dependency loops), plus genuine
    self-recursive single nodes (a node with a self-edge).

    Excludes `inferred` and `hook` edges: a reported circular dependency must be
    trustworthy, and real recursion/mutual-recursion resolves as exact. This drops
    phantom self-loops (self.db.close) and string-dispatch hook edges."""
    edges = store.all_edges(exclude_resolutions=("inferred", "hook"))
    comps = [c for c in sccs(edges) if len(c) >= min_size]
    if min_size <= 1:
        return comps
    self_loops = {s for s, d in edges if s == d}
    comps.extend([[s] for s in self_loops])
    return comps



def _brandes(nodes: list[int], adj: dict[int, list[int]]) -> dict[int, float]:
    bc = {n: 0.0 for n in nodes}
    for s in nodes:
        stack = []
        pred = {w: [] for w in nodes}
        sigma = dict.fromkeys(nodes, 0.0); sigma[s] = 1.0
        dist = dict.fromkeys(nodes, -1); dist[s] = 0
        q = deque([s])
        while q:
            v = q.popleft(); stack.append(v)
            for w in adj.get(v, ()):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                if sigma[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    return bc


# A re-declared core/library function (WP `__`/`apply_filters`, a vendor polyfill) is defined
# once but called from a huge fraction of the repo, dwarfing real hotspots. Flag a symbol as a
# suspected shadow when its fan-in is both a large share of ALL call edges and above an
# absolute floor - framework-agnostic, repo-size-aware. FLAG, never drop (a legit hot utility
# could trip it; the user/agent reviews the flag and excludes the defining file if it's vendor).
SHADOW_FANIN_FRACTION = 0.03
SHADOW_FANIN_MIN = 100


def hotspots(store: Store, top: int = 20, betweenness_limit: int = 1500) -> dict:
    # Count DISTINCT CALL SITES (ref_id), not edge rows: one ambiguous call fanned to
    # N candidates would otherwise inflate N symbols' fan-in from a single site and
    # distort both ranking and the shadow fraction. Hook edges have NULL ref_id ->
    # fall back to the edge rowid (each is its own site). Also track DEPENDENTS
    # (distinct calling symbols) so one repetitive caller is visibly one dependent.
    rows = store.db.execute(
        "SELECT src, dst, COALESCE(ref_id, -rowid) rid FROM edges WHERE src IS NOT NULL").fetchall()
    in_sites = defaultdict(set)
    out_sites = defaultdict(set)
    dep_syms = defaultdict(set)
    adj = defaultdict(list)
    nodes = set()
    seen_pairs = set()
    for r in rows:
        s, d = r["src"], r["dst"]
        out_sites[s].add(r["rid"])
        in_sites[d].add(r["rid"])
        dep_syms[d].add(s)
        if (s, d) not in seen_pairs:
            seen_pairs.add((s, d))
            adj[s].append(d)
        nodes.add(s); nodes.add(d)
    nodes = list(nodes)
    fan_in = {k: len(v) for k, v in in_sites.items()}
    fan_out = {k: len(v) for k, v in out_sites.items()}

    total_edges = sum(fan_in.values())
    shadow_cut = max(SHADOW_FANIN_MIN, SHADOW_FANIN_FRACTION * total_edges)

    def is_shadow(sid):
        return total_edges > 0 and fan_in.get(sid, 0) >= shadow_cut

    bc = {}
    if 0 < len(nodes) <= betweenness_limit:
        bc = _brandes(nodes, adj)

    def render(rank: list[tuple[int, float]]):
        out = []
        for sid, score in rank[:top]:
            s = store.symbol_by_id(sid)
            if not s:
                continue
            out.append({
                "symbol": _label(s), "file": s["file"], "line": s["start_line"],
                "fan_in": fan_in.get(sid, 0), "fan_out": fan_out.get(sid, 0),
                "dependents": len(dep_syms.get(sid, ())), "score": round(score, 3),
                "suspected_shadow": is_shadow(sid),
            })
        return out

    by_fan_in = sorted(fan_in.items(), key=lambda kv: kv[1], reverse=True)
    result = {
        "most_depended_upon": render([(k, float(v)) for k, v in by_fan_in]),
        "betweenness_computed": bool(bc),
    }
    if bc:
        by_bc = sorted(bc.items(), key=lambda kv: kv[1], reverse=True)
        result["most_central"] = render(by_bc)
    shadow_files = sorted({e["file"] for e in result["most_depended_upon"] if e["suspected_shadow"]})
    if shadow_files:
        result["suspected_shadow_note"] = (
            f"{len(shadow_files)} file(s) define symbols called from a huge fraction of the repo - "
            f"likely re-declared core/library functions (vendor shims), NOT real hotspots. Exclude "
            f"them via scope() so hotspots/blast-radius reflect real code: {shadow_files}")
    return result



def build_adj(store: Store) -> dict[int, list[int]]:
    adj = defaultdict(list)
    for s, d in store.all_edges():
        adj[s].append(d)
    return adj


def shortest_path(store: Store, src: int, dst: int, adj: dict | None = None) -> list[int] | None:
    if src == dst:
        return [src]
    if adj is None:
        adj = build_adj(store)
    prev = {src: None}
    q = deque([src])
    while q:
        v = q.popleft()
        for w in adj[v]:
            if w not in prev:
                prev[w] = v
                if w == dst:
                    path = [dst]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                q.append(w)
    return None



def resolved_out_edges(store: Store, path: str) -> set[tuple[str, str]]:
    """Resolved outgoing call edges of a file as {(src_label, dst_label)}, both
    container-qualified - so a re-pointed call (Foo.save -> Bar.save) differs even
    when the callee name is unchanged."""
    path = os.path.realpath(path)  # graph stores realpaths; an alias spelling must still match
    rows = store.db.execute(
        "SELECT src.name sn, src.container sc, dst.name dn, dst.container dc "
        "FROM edges e LEFT JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst "
        "WHERE e.file=?", (path,)).fetchall()
    out = set()
    for r in rows:
        src = _label_parts(r["sn"], r["sc"]) if r["sn"] else "<module>"
        out.add((src, _label_parts(r["dn"], r["dc"])))
    return out


def _edges_by_file(store: Store) -> dict[str, set]:
    """Every file's resolved out-edges (WITH resolution tier) in one pass - the GLOBAL
    before/after snapshot for pending(). The tier is part of the tuple so a re-resolution
    that flips exact -> ambiguous (same names, e.g. a new same-named def elsewhere)
    still surfaces as a delta instead of vanishing into an identical label pair."""
    out: dict[str, set] = defaultdict(set)
    rows = store.db.execute(
        "SELECT e.file f, e.resolution res, src.name sn, src.container sc, dst.name dn, dst.container dc "
        "FROM edges e LEFT JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst").fetchall()
    for r in rows:
        src = _label_parts(r["sn"], r["sc"]) if r["sn"] else "<module>"
        out[r["f"]].add((src, _label_parts(r["dn"], r["dc"]), r["res"]))
    return out


def pending(store: Store, root: str, db_path: str) -> dict:
    """Diff RESOLVED call edges (caller -> resolved target, container-qualified)
    before vs after folding on-disk changes in. GLOBAL diff, not changed-files-only:
    re-resolution is repo-wide, so an edit that adds a same-named symbol can flip
    ANOTHER file's edges (exact -> ambiguous) - those deltas are reported with
    state 'reresolved'."""
    root = os.path.realpath(root)
    changed = I.changed_files(root, db_path)
    if not changed:
        return {"root": root, "changed_files": 0, "deltas": []}
    states = dict(changed)
    before = _edges_by_file(store)
    res = I.index_safe(root, db_path)  # re-resolve so the "after" edges are real
    if res.get("locked"):
        return {"root": root, "changed_files": len(changed), "deltas": [], **res}
    deltas = []
    after_store = Store(db_path)
    try:
        after = _edges_by_file(after_store)
    finally:
        after_store.close()
    for path in sorted(set(before) | set(after) | set(states)):
        state = states.get(path, "reresolved")
        aft = set() if state == "deleted" else after.get(path, set())
        bef = before.get(path, set())
        added = sorted(f"{a} -> {b} [{res}]" for a, b, res in (aft - bef))
        removed = sorted(f"{a} -> {b} [{res}]" for a, b, res in (bef - aft))
        if added or removed or state in ("new", "deleted"):
            deltas.append({"file": os.path.relpath(path, root), "state": state,
                           "added": added, "removed": removed})
    return {"root": root, "changed_files": len(deltas), "deltas": deltas}



def _label_parts(name: str, container: str | None) -> str:
    return f"{container}.{name}" if container else name


def _label(s) -> str:
    return _label_parts(s["name"], s["container"])
