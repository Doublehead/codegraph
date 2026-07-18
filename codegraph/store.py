"""SQLite-backed graph store.

symbols  - every definition (function/method/class/...)
refs     - every raw call site (callee name + receiver + the symbol it sits in)
edges    - resolved caller->callee links, tagged exact|ambiguous
unresolved - call sites whose target wasn't found (kept honest, not dropped)

Resolution is rebuilt globally after any file changes, because a new/removed
definition in one file can change how another file's calls resolve. It's an
in-memory dict pass - cheap even on large repos.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict

from .parser import ParsedFile
from . import languages as L

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    lang TEXT,
    hash TEXT,
    mtime REAL,
    size INTEGER,
    indexed_at REAL,
    warnings TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file TEXT,
    name TEXT,
    kind TEXT,
    container TEXT,
    lang TEXT,
    start_line INTEGER,
    end_line INTEGER,
    start_byte INTEGER,
    signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file);
CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY,
    file TEXT,
    lang TEXT,
    callee TEXT,
    receiver TEXT,
    line INTEGER,
    src_symbol INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ref_file ON refs(file);
CREATE INDEX IF NOT EXISTS idx_ref_callee ON refs(callee);
CREATE TABLE IF NOT EXISTS edges (
    src INTEGER,
    dst INTEGER,
    kind TEXT,
    resolution TEXT,
    file TEXT,
    line INTEGER,
    ref_id INTEGER     -- the originating call site (refs.id); NULL for synthesized hook edges
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS unresolved (
    file TEXT,
    lang TEXT,
    callee TEXT,
    receiver TEXT,
    line INTEGER,
    src_symbol INTEGER
);
CREATE TABLE IF NOT EXISTS imports (
    file TEXT,
    module TEXT
);
CREATE INDEX IF NOT EXISTS idx_imp_file ON imports(file);
CREATE TABLE IF NOT EXISTS assigns (
    file TEXT,
    lang TEXT,
    scope_symbol INTEGER,
    var TEXT,
    cls TEXT,
    kind TEXT          -- construct = proven (x=Foo()); hint = declared (x: Foo)
);
CREATE INDEX IF NOT EXISTS idx_asg_file ON assigns(file);
CREATE TABLE IF NOT EXISTS hooks (
    id INTEGER PRIMARY KEY,
    file TEXT,
    lang TEXT,
    kind TEXT,              -- listen | fire
    hook TEXT,             -- hook name / REST route; NULL if dynamic
    hook_class TEXT,        -- action|filter|ajax|ajax_nopriv|rest|fire
    enclosing_symbol INTEGER,
    cb_kind TEXT, cb_name TEXT, cb_container TEXT, cb_recv TEXT, cb_raw TEXT,
    callback_symbol INTEGER,   -- resolved listener (NULL for fire/closure/dynamic/unresolved)
    entry_point INTEGER, unauth INTEGER, note TEXT, line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_hook_file ON hooks(file);
CREATE INDEX IF NOT EXISTS idx_hook_name ON hooks(hook);
CREATE INDEX IF NOT EXISTS idx_hook_cb ON hooks(callback_symbol);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

SCHEMA_VERSION = 2  # bump for a STRUCTURAL change add-column migration can't express

# Every column the engine INSERTs, per table - _migrate ensures each exists on an older DB
# (id/PRIMARY-KEY columns are part of CREATE and never ALTER-added). Keep in sync with the
# INSERT statements; a missing column here is the exact 'no column named X' crash class.
_EXPECTED_COLUMNS = {
    "files": {"path": "TEXT", "lang": "TEXT", "hash": "TEXT", "mtime": "REAL",
              "size": "INTEGER", "indexed_at": "REAL", "warnings": "TEXT"},
    "symbols": {"file": "TEXT", "name": "TEXT", "kind": "TEXT", "container": "TEXT", "lang": "TEXT",
                "start_line": "INTEGER", "end_line": "INTEGER", "start_byte": "INTEGER", "signature": "TEXT"},
    "refs": {"file": "TEXT", "lang": "TEXT", "callee": "TEXT", "receiver": "TEXT",
             "line": "INTEGER", "src_symbol": "INTEGER"},
    "edges": {"src": "INTEGER", "dst": "INTEGER", "kind": "TEXT", "resolution": "TEXT",
              "file": "TEXT", "line": "INTEGER", "ref_id": "INTEGER"},
    "unresolved": {"file": "TEXT", "lang": "TEXT", "callee": "TEXT", "receiver": "TEXT",
                   "line": "INTEGER", "src_symbol": "INTEGER"},
    "imports": {"file": "TEXT", "module": "TEXT"},
    "assigns": {"file": "TEXT", "lang": "TEXT", "scope_symbol": "INTEGER", "var": "TEXT",
                "cls": "TEXT", "kind": "TEXT"},
    "hooks": {"file": "TEXT", "lang": "TEXT", "kind": "TEXT", "hook": "TEXT", "hook_class": "TEXT",
              "enclosing_symbol": "INTEGER", "cb_kind": "TEXT", "cb_name": "TEXT", "cb_container": "TEXT",
              "cb_recv": "TEXT", "cb_raw": "TEXT", "callback_symbol": "INTEGER", "entry_point": "INTEGER",
              "unauth": "INTEGER", "note": "TEXT", "line": "INTEGER"},
}


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        # WAL = concurrent readers + one writer; busy_timeout makes a contended
        # writer wait instead of raising "database is locked". Required once edits
        # fire parallel re-index/query (batched tool calls, hooks).
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Bring an older graph.db up to the current schema. CREATE TABLE IF NOT EXISTS
        won't add columns to a pre-existing table, so any column added after a table was
        first created (e.g. hooks.cb_recv) is missing on old DBs and an INSERT throws
        'no column named X'. Comprehensively ALTER-ADD every column the code inserts that's
        absent - robust to ALL past/future column additions, not whack-a-mole. Data is
        preserved (new columns are NULL until the next index rebuilds those rows). A future
        STRUCTURAL change that add-column can't express would bump SCHEMA_VERSION and rebuild."""
        changed = False
        for table, cols in _EXPECTED_COLUMNS.items():
            existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if not existing:  # table absent -> executescript(SCHEMA) already made it complete
                continue
            for col, decl in cols.items():
                if col not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    changed = True
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            changed = True
        if changed:
            self.db.commit()

    def close(self):
        self.db.close()


    def file_hash(self, path: str) -> str | None:
        row = self.db.execute("SELECT hash FROM files WHERE path=?", (path,)).fetchone()
        return row["hash"] if row else None

    def file_sig(self, path: str):
        """(mtime, hash, size) for a known file, else None - the incremental fast path
        requires mtime AND size to match (a same-mtime-tick content change almost
        always moves the byte count; without the tiebreak it would be skipped stale
        forever). size is None on rows from before the column existed."""
        row = self.db.execute("SELECT mtime,hash,size FROM files WHERE path=?", (path,)).fetchone()
        return (row["mtime"], row["hash"], row["size"]) if row else None

    def touch_mtime(self, path: str, mtime: float, size: int | None = None) -> None:
        self.db.execute("UPDATE files SET mtime=?, size=COALESCE(?, size) WHERE path=?",
                        (mtime, size, path))
        self.db.commit()

    def _site_counts(self) -> dict:
        """Resolution quality by CALL SITE, not fanned-out edge rows. An ambiguous call to
        a name with N in-repo candidates is ONE ambiguous decision (but N edge rows);
        exact/inferred are 1:1 with rows. Counting rows over-weights ambiguity (the 174x
        index-vs-stats gap on a vendor monorepo). Matches resolve()'s per-call counters so
        index and stats agree. hook stays a row count (small, and a fire->N-listeners fan
        is meaningful). edge_rows is the raw candidate-edge total (graph size)."""
        # DISTINCT ref_id = distinct originating call sites (a ref fans to N candidate rows
        # but is ONE decision); robust to several calls sharing a source line, which a
        # (src,file,line) key would collapse. ref_id is NULL only on hook edges.
        site = lambda res: self.db.execute(
            "SELECT COUNT(DISTINCT ref_id) FROM edges WHERE resolution=?", (res,)).fetchone()[0]
        g = lambda q: self.db.execute(q).fetchone()[0]
        return {
            "edges_exact": site("exact"),
            "edges_inferred": site("inferred"),
            "edges_ambiguous": site("ambiguous"),
            "edges_hook": g("SELECT COUNT(*) FROM edges WHERE resolution='hook'"),
            "unresolved": g("SELECT COUNT(*) FROM unresolved"),
            "edge_rows": g("SELECT COUNT(*) FROM edges"),
        }

    def resolution_counts(self) -> dict:
        return self._site_counts()

    def known_files(self) -> set[str]:
        return {r["path"] for r in self.db.execute("SELECT path FROM files")}

    def mark_resolve_pending(self) -> None:
        """Persist (COMMITTED) that file facts are about to mutate ahead of the edge
        rebuild. If the process dies in that window, the flag survives and the next
        index re-resolves instead of trusting edges that reference recycled symbol
        rowids or were never rebuilt. Cleared inside resolve()'s own commit."""
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('resolve_pending','1')")
        self.db.commit()

    def resolve_pending(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM meta WHERE key='resolve_pending'").fetchone() is not None

    def forget_file(self, path: str) -> None:
        for tbl in ("symbols", "refs", "unresolved", "imports", "assigns", "hooks", "files"):
            self.db.execute(f"DELETE FROM {tbl} WHERE {'path' if tbl=='files' else 'file'}=?", (path,))

    def index_file(self, path: str, file_hash: str, mtime: float, indexed_at: float, pf: ParsedFile,
                   size: int | None = None) -> None:
        """Replace all stored facts for one file. Edges are rebuilt by resolve()."""
        self.forget_file(path)
        self.db.execute(
            "INSERT INTO files(path,lang,hash,mtime,size,indexed_at,warnings) VALUES(?,?,?,?,?,?,?)",
            (path, pf.lang, file_hash, mtime, size, indexed_at, "\n".join(pf.warnings) or None),
        )
        byte_to_id: dict[int, int] = {}
        for s in pf.symbols:
            cur = self.db.execute(
                "INSERT INTO symbols(file,name,kind,container,lang,start_line,end_line,start_byte,signature)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (path, s.name, s.kind, s.container, pf.lang, s.start_line, s.end_line, s.start_byte, s.signature),
            )
            byte_to_id[s.start_byte] = cur.lastrowid
        # Symbols are inserted per-row above (each needs its rowid for byte_to_id). Everything
        # downstream is batched with executemany - one round-trip per table instead of one per
        # row, the dominant write cost on a large vendor file. Same data, just fewer calls.
        bget = byte_to_id.get
        self.db.executemany(
            "INSERT INTO refs(file,lang,callee,receiver,line,src_symbol) VALUES(?,?,?,?,?,?)",
            [(path, pf.lang, r.callee, r.receiver, r.line,
              bget(r.src_byte) if r.src_byte is not None else None) for r in pf.refs])
        self.db.executemany("INSERT INTO imports(file,module) VALUES(?,?)",
                            [(path, mod) for mod in pf.imports])
        self.db.executemany(
            "INSERT INTO assigns(file,lang,scope_symbol,var,cls,kind) VALUES(?,?,?,?,?,?)",
            [(path, pf.lang, bget(a.src_byte) if a.src_byte is not None else None, a.var, a.cls, a.kind)
             for a in pf.assigns])
        self.db.executemany(
            "INSERT INTO hooks(file,lang,kind,hook,hook_class,enclosing_symbol,cb_kind,cb_name,"
            "cb_container,cb_recv,cb_raw,callback_symbol,entry_point,unauth,note,line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(path, pf.lang, h.kind, h.hook, h.hook_class,
              bget(h.src_byte) if h.src_byte is not None else None, h.cb_kind, h.cb_name,
              h.cb_container, h.cb_recv, h.cb_raw, None, int(h.entry_point), int(h.unauth), h.note, h.line)
             for h in pf.hooks])
        self.db.commit()


    def resolve(self) -> dict:
        """Rebuild edges + unresolved from all refs, resolving by CALL SHAPE, not
        name alone. Each call's receiver decides what it can target (free function,
        same-class method, parent method, a known class's static method, or a
        typed/untyped instance method); resolution is filtered accordingly.

        Confidence is honest, three-tier:
          exact     -> a single target the shape proves (bare->free fn, self->same
                       class, typed instance/static->that class, constructor).
          inferred  -> a single plausible target but the receiver type is unverified
                       (untyped instance, super/parent, inherited self). Disclosed,
                       never silently 'exact'.
          ambiguous -> multiple candidates; edges to all, so reachability stays sound.
        A call with no plausible in-repo target is recorded unresolved, not faked."""
        self.db.execute("DELETE FROM edges")
        self.db.execute("DELETE FROM unresolved")

        # Index candidates several ways so resolution is O(1), not an O(n) scan of
        # every same-named symbol - the difference between linear and quadratic on
        # large repos where a method name recurs across many classes.
        syms = self.db.execute("SELECT id,file,name,kind,container,lang FROM symbols").fetchall()
        by_name: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        free_by: dict[tuple[str, str], list] = defaultdict(list)
        meth_by: dict[tuple[str, str], list] = defaultdict(list)
        class_by: dict[tuple[str, str], list] = defaultdict(list)
        meth_by_nc: dict[tuple[str, str, str], list] = defaultdict(list)  # (lang,name,container)
        class_names: set[tuple[str, str]] = set()
        src_container: dict[int, str | None] = {}
        name_by_id: dict[int, str] = {}
        for s in syms:
            lf = _fam(s["lang"])  # .ts/.tsx/.js share one resolution namespace (they import each other)
            k = (lf, s["name"])
            by_name[k].append(s)
            src_container[s["id"]] = s["container"]
            name_by_id[s["id"]] = s["name"]
            if s["kind"] == "class":
                class_names.add(k)
                class_by[k].append(s)
            if _is_free(s):
                free_by[k].append(s)
            elif _is_method(s):
                meth_by[k].append(s)
                meth_by_nc[(lf, s["name"], s["container"])].append(s)

        # Value is (cls, kind). kind: 'construct' (proven, x=Foo()) | 'hint' (declared,
        # x: Foo). Collect EVERY type asserted for a (scope, var); a var assigned
        # CONFLICTING classes - a closure-local shadow leaking into its parent, or a
        # reassignment to a different type - can't be pinned to one class, so it drops
        # to None and the receiver resolves via the honest ambiguous pool instead of a
        # WRONG exact (and no real caller is lost). Same class from a hint + a construct
        # collapses to the construct (proof beats a declared contract).
        # Also track which names are bound LOCALLY in a scope (any kind, incl. typeless
        # 'shadow' params/loops/assignments). A locally-bound name masks a same-named
        # global class or module construct, so the resolver won't fall back to those.
        raw_scope: dict[tuple[int, str], set] = defaultdict(set)
        raw_file: dict[tuple[str, str], set] = defaultdict(set)
        bound_scope: set[tuple[int, str]] = set()
        bound_file: set[tuple[str, str]] = set()
        free_names = set(free_by)  # (lang,name) that have a free function
        # (lang, class_name) -> module names mixed in via Ruby include/extend/prepend;
        # consulted by bare-call lookup inside that class (MRO before top-level defs).
        mixins: dict[tuple[str, str], set] = defaultdict(set)
        for a in self.db.execute("SELECT file,lang,scope_symbol,var,cls,kind FROM assigns"):
            if a["kind"] == "mixin":  # not a var binding - a class<-module wiring fact
                cname = name_by_id.get(a["scope_symbol"]) if a["scope_symbol"] is not None else None
                if cname:
                    mixins[(_fam(a["lang"]), cname)].add(a["cls"])
                continue
            if a["scope_symbol"] is not None:
                bound_scope.add((a["scope_symbol"], a["var"]))
            else:
                bound_file.add((a["file"], a["var"]))
            if a["kind"] == "shadow":
                continue  # records local binding only, asserts no type
            kind = a["kind"] or "construct"
            # Python `x = Name()` is ambiguous when Name is BOTH a class and a free
            # function - it may be a call, not construction. Demote to a hint so the
            # receiver resolves `inferred`, never a wrong `exact`.
            if kind == "construct" and a["lang"] == "python" and (a["lang"], a["cls"]) in free_names:
                kind = "hint"
            val = (a["cls"], kind)
            if a["scope_symbol"] is not None:
                raw_scope[(a["scope_symbol"], a["var"])].add(val)
            else:
                raw_file[(a["file"], a["var"])].add(val)
        assign_scope = {k: c for k, s in raw_scope.items() if (c := _collapse_types(s))}
        assign_file = {k: c for k, s in raw_file.items() if (c := _collapse_types(s))}

        # Per-file imported names, so `import util; util.helper()` resolves against
        # util's file instead of landing unresolved (Python's dominant cross-module
        # call shape). For path-style specifiers (go "app/store", js "./util") the
        # tail is also indexed - it's the name the receiver actually uses.
        imports_by_file: dict[str, set] = defaultdict(set)
        for i in self.db.execute("SELECT file,module FROM imports"):
            m = i["module"]
            s = imports_by_file[i["file"]]
            s.add(m)
            if "/" in m or m.startswith("."):
                t = m.rsplit("/", 1)[-1].split(".")[0]
                if t:
                    s.add(t)

        counts = {"exact": 0, "inferred": 0, "ambiguous": 0, "unresolved": 0}
        edge_rows, unres_rows = [], []
        for r in self.db.execute("SELECT id,file,lang,callee,receiver,line,src_symbol FROM refs"):
            lf = _fam(r["lang"])  # resolve a .tsx caller against .ts/.js defs and vice versa
            k = (lf, r["callee"])
            if k not in by_name:
                unres_rows.append((r["file"], r["lang"], r["callee"], r["receiver"], r["line"], r["src_symbol"]))
                counts["unresolved"] += 1
                continue
            # Callee-side masking, mirroring the receiver-side rule: a bare call
            # through a name bound LOCALLY (param, local/module var) targets a runtime
            # value, not the same-named global - disclose unresolved, never a wrong
            # exact to another file's function. js-only carve-out: there the binding
            # may BE a same-file definition (`const f = () => {}` is extracted as both
            # symbol and shadow; symbols don't record nesting), so a same-file
            # candidate keeps normal resolution. Elsewhere (py/go/ruby) defs are never
            # assignment shadows, so a binding always masks. PHP fully exempt via
            # LOCALS_SHADOW_FUNCS ($-sigiled vars can't shadow functions).
            if not (r["receiver"] or "").strip() and lf in L.LOCALS_SHADOW_FUNCS:
                bound = ((r["src_symbol"], r["callee"]) in bound_scope
                         if r["src_symbol"] is not None else False) \
                        or (r["file"], r["callee"]) in bound_file
                if bound and not (lf == "js" and any(
                        c["file"] == r["file"] for c in free_by[k] + class_by[k])):
                    unres_rows.append((r["file"], r["lang"], r["callee"], r["receiver"], r["line"], r["src_symbol"]))
                    counts["unresolved"] += 1
                    continue
            shape, tclass = _classify(r["receiver"], lf, r["src_symbol"],
                                      class_names, assign_scope, assign_file, r["file"],
                                      bound_scope, bound_file,
                                      imports_by_file.get(r["file"], frozenset()))
            scv = src_container.get(r["src_symbol"]) if r["src_symbol"] is not None else None
            chosen, conf = _resolve_shape(shape, tclass, r["file"], scv, lf, r["callee"],
                                          free_by[k], meth_by[k], class_by[k], meth_by_nc,
                                          mixins)
            if not chosen:
                unres_rows.append((r["file"], r["lang"], r["callee"], r["receiver"], r["line"], r["src_symbol"]))
                counts["unresolved"] += 1
                continue
            counts[conf] += 1
            for c in chosen:
                edge_rows.append((r["src_symbol"], c["id"], "calls", conf, r["file"], r["line"], r["id"]))

        self.db.executemany(
            "INSERT INTO edges(src,dst,kind,resolution,file,line,ref_id) VALUES(?,?,?,?,?,?,?)", edge_rows)
        self.db.executemany(
            "INSERT INTO unresolved(file,lang,callee,receiver,line,src_symbol) VALUES(?,?,?,?,?,?)", unres_rows)

        # --- WordPress hook resolution (string dispatch the call graph can't see) ---
        n_hook = self._resolve_hooks(free_by, meth_by, meth_by_nc, src_container,
                                     assign_scope, assign_file)

        # Clear the crash-window flag INSIDE this commit: edges are rebuilt and the
        # pending marker drops atomically. A kill anywhere before this line leaves
        # the flag set, so the next index re-resolves instead of trusting a graph
        # whose committed file facts never got their edges rebuilt.
        self.db.execute("DELETE FROM meta WHERE key='resolve_pending'")
        self.db.commit()
        # edges_* are CALL SITES (one ambiguous call to N candidates is ONE ambiguous
        # decision); edge_rows is the fanned-out candidate-edge total (graph size).
        return {"edges_exact": counts["exact"], "edges_inferred": counts["inferred"],
                "edges_ambiguous": counts["ambiguous"], "edges_hook": n_hook,
                "unresolved": counts["unresolved"], "edge_rows": len(edge_rows) + n_hook}

    def _resolve_hooks(self, free_by, meth_by, meth_by_nc, src_container,
                       assign_scope, assign_file) -> int:
        """Resolve each listener's callback to a symbol, then synthesize fire->callback
        edges (resolution='hook') for in-repo do_action/apply_filters sites. The
        callback->hook direction (stored on each row) is the high-confidence half."""
        self.db.execute("UPDATE hooks SET callback_symbol=NULL")
        # Python precision gate: only treat `.connect`/`.send`/`.delay` as dispatch when the
        # name is EVIDENCED - a Django builtin signal, an in-repo `x = Signal()`, a @receiver
        # target, or a @task name. Without this, a non-signal `sock.connect(fn)`+`sock.send()`
        # or a stray `obj.delay()` fabricates a hook edge to a real function. Decorator-based
        # rows (@receiver/@task) and route entry points are always trusted (they ARE evidence).
        confirmed_py = set(L.DJANGO_BUILTIN_SIGNALS)
        for a in self.db.execute("SELECT DISTINCT var FROM assigns WHERE lang='python' AND cls='Signal'"):
            confirmed_py.add(a["var"])
        for r in self.db.execute(
                "SELECT DISTINCT hook FROM hooks WHERE lang='python' AND kind='listen' AND note IN ('@receiver','@task')"):
            if r["hook"]:
                confirmed_py.add(r["hook"])

        def _py_signal_ok(lang, hook, note):
            return lang != "python" or note in ("@receiver", "@task", "route") or hook in confirmed_py

        def _mech(lang, tag):
            # Python signals and tasks fire in DISTINCT namespaces (a `.send()` must not reach
            # a same-named Celery task, nor a `.delay()` a same-named signal receiver). WP
            # actions/filters stay flat (one namespace, unchanged). `tag` = listener hook_class
            # or fire note.
            return tag if (lang == "python" and tag in ("signal", "task")) else ""

        hook_listeners = defaultdict(list)  # (lang,hook) -> [callback_symbol_id]
        for h in self.db.execute("SELECT * FROM hooks WHERE kind='listen'").fetchall():
            if not _py_signal_ok(h["lang"], h["hook"], h["note"]):
                continue
            cb_id = self._resolve_callback(h, free_by, meth_by, meth_by_nc, src_container,
                                           assign_scope, assign_file)
            if cb_id is not None:
                self.db.execute("UPDATE hooks SET callback_symbol=? WHERE id=?", (cb_id, h["id"]))
                # Routes are framework-fired entry points, not joined to in-repo fires.
                if h["hook"] and h["hook_class"] != "route":
                    hook_listeners[(h["lang"], _mech(h["lang"], h["hook_class"]), h["hook"])].append(cb_id)
        edges = []
        for f in self.db.execute("SELECT lang,enclosing_symbol,hook,file,line,note FROM hooks WHERE kind='fire'"):
            if not f["hook"] or f["enclosing_symbol"] is None:
                continue
            if not _py_signal_ok(f["lang"], f["hook"], f["note"]):
                continue
            for cb_id in hook_listeners.get((f["lang"], _mech(f["lang"], f["note"]), f["hook"]), []):
                edges.append((f["enclosing_symbol"], cb_id, "hook", "hook", f["file"], f["line"]))
        self.db.executemany(
            "INSERT INTO edges(src,dst,kind,resolution,file,line) VALUES(?,?,?,?,?,?)", edges)
        return len(edges)

    @staticmethod
    def _resolve_callback(h, free_by, meth_by, meth_by_nc, src_container, assign_scope, assign_file):
        """Resolve a WP callback to a symbol id. Bias: never return a WRONG symbol -
        if a container is named but absent, or a bare method name is ambiguous, return
        None rather than guessing the first same-named method."""
        lang, name, kind, cont = h["lang"], h["cb_name"], h["cb_kind"], h["cb_container"]
        if not name:
            return None  # closure / dynamic
        if kind == "free":
            c = free_by.get((lang, name)) or free_by.get((lang, name.rsplit("\\", 1)[-1])) or []
            c = _pref(c, h["file"])  # same-file registration is the overwhelming WP shape
            return c[0]["id"] if len(c) == 1 else None
        if kind == "method_self":  # [$this,'m'] / self::class -> the enclosing class
            cont = src_container.get(h["enclosing_symbol"]) if h["enclosing_symbol"] is not None else None
        elif kind == "method" and h["cb_recv"]:  # [$obj,'m'] -> local type inference
            enc = h["enclosing_symbol"]
            hit = (assign_scope.get((enc, h["cb_recv"])) if enc is not None else None) \
                or assign_file.get((h["file"], h["cb_recv"]))
            cont = hit[0] if hit else None  # assign value is (cls, kind)
        if cont:
            c = meth_by_nc.get((lang, name, cont))  # exact container match (incl. a stored FQN)
            if c:
                c = _pref(c, h["file"])  # duplicated class (vendor shadow copy) -> unique only
                return c[0]["id"] if len(c) == 1 else None
            # Fall back to the bare class name. Symbols are stored bare, so a
            # namespaced callback (\A\Mailer) that collides with another bare
            # Mailer is indistinguishable here -> resolve ONLY if unique, never guess.
            bare = cont.rsplit("\\", 1)[-1]
            c = meth_by_nc.get((lang, name, bare)) or []
            return c[0]["id"] if len(c) == 1 else None
        c = meth_by.get((lang, name)) or []  # unknown receiver type -> resolve only if unique
        if len(c) == 1:
            return c[0]["id"]
        if not c and lang == "python":  # Django `views.list_users`: module.func is a FREE function
            fc = free_by.get((lang, name)) or []
            return fc[0]["id"] if len(fc) == 1 else None
        return None


    def symbol_by_id(self, sid: int):
        return self.db.execute("SELECT * FROM symbols WHERE id=?", (sid,)).fetchone()

    def find(self, name: str, lang: str | None = None, kind: str | None = None):
        q = "SELECT * FROM symbols WHERE name=?"
        args = [name]
        if lang:
            q += " AND lang=?"; args.append(lang)
        if kind:
            q += " AND kind=?"; args.append(kind)
        q += " ORDER BY file, start_line"
        return self.db.execute(q, args).fetchall()

    def reachable(self, sid: int, direction: str, max_depth: int) -> list[tuple[int, int]]:
        """direction 'callers' = reverse (who reaches sid); 'callees' = forward."""
        if direction == "callers":
            step = "SELECT e.src AS nxt, w.depth+1 FROM edges e JOIN walk w ON e.dst=w.id WHERE e.src IS NOT NULL"
        else:
            step = "SELECT e.dst AS nxt, w.depth+1 FROM edges e JOIN walk w ON e.src=w.id WHERE e.dst IS NOT NULL"
        sql = f"""
        WITH RECURSIVE walk(id, depth) AS (
            SELECT ?, 0
            UNION
            {step} AND w.depth < ?
        )
        SELECT id, MIN(depth) AS depth FROM walk WHERE id <> ? GROUP BY id ORDER BY depth
        """
        return [(r["id"], r["depth"]) for r in self.db.execute(sql, (sid, max_depth, sid))]

    def neighbors(self, sid: int):
        out = self.db.execute(
            "SELECT s.*, e.resolution, e.line AS call_line FROM edges e JOIN symbols s ON s.id=e.dst "
            "WHERE e.src=?", (sid,)).fetchall()
        inc = self.db.execute(
            "SELECT s.*, e.resolution, e.line AS call_line FROM edges e JOIN symbols s ON s.id=e.src "
            "WHERE e.dst=? AND e.src IS NOT NULL", (sid,)).fetchall()
        return inc, out

    def all_edges(self, exclude_resolutions: tuple = ()) -> list[tuple[int, int]]:
        sql = "SELECT src,dst FROM edges WHERE src IS NOT NULL"
        args: list = []
        if exclude_resolutions:
            sql += " AND resolution NOT IN (%s)" % ",".join("?" * len(exclude_resolutions))
            args = list(exclude_resolutions)
        return [(r["src"], r["dst"]) for r in self.db.execute(sql, args)]


    def hooks_for_symbol(self, sid: int):
        """Listener rows whose callback is this symbol - 'this function fires on hook X'."""
        return self.db.execute(
            "SELECT hook, hook_class, entry_point, unauth, note, file, line FROM hooks "
            "WHERE kind='listen' AND callback_symbol=? ORDER BY hook", (sid,)).fetchall()

    def listeners_of_hook(self, hook: str):
        return self.db.execute(
            "SELECT h.*, s.name cb_sym_name, s.container cb_sym_cont, s.file cb_file, s.start_line cb_line "
            "FROM hooks h LEFT JOIN symbols s ON s.id=h.callback_symbol "
            "WHERE h.kind='listen' AND h.hook=? ORDER BY h.file,h.line", (hook,)).fetchall()

    def fires_of_hook(self, hook: str):
        return self.db.execute(
            "SELECT file, line, enclosing_symbol FROM hooks WHERE kind='fire' AND hook=? ORDER BY file,line",
            (hook,)).fetchall()

    def entry_points(self):
        return self.db.execute(
            "SELECT h.*, s.name cb_sym_name, s.container cb_sym_cont, s.file cb_file, s.start_line cb_line "
            "FROM hooks h LEFT JOIN symbols s ON s.id=h.callback_symbol "
            "WHERE h.kind='listen' AND h.entry_point=1 ORDER BY h.unauth DESC, h.hook_class, h.hook").fetchall()

    def stats(self) -> dict:
        g = lambda q: self.db.execute(q).fetchone()[0]
        langs = self.db.execute(
            "SELECT lang, COUNT(*) n FROM symbols GROUP BY lang ORDER BY n DESC"
        ).fetchall()
        kinds = self.db.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        sc = self._site_counts()  # edges_* are CALL SITES; edge_rows is the fanned-out total
        return {
            "files": g("SELECT COUNT(*) FROM files"),
            "symbols": g("SELECT COUNT(*) FROM symbols"),
            "edge_rows": sc["edge_rows"],          # total candidate edges (graph size)
            "edges_exact": sc["edges_exact"],      # the rest are by CALL SITE, matching index()
            "edges_inferred": sc["edges_inferred"],
            "edges_ambiguous": sc["edges_ambiguous"],
            "edges_hook": sc["edges_hook"],
            "hook_registrations": g("SELECT COUNT(*) FROM hooks WHERE kind='listen'"),
            "entry_points": g("SELECT COUNT(*) FROM hooks WHERE entry_point=1"),
            "unauth_entry_points": g("SELECT COUNT(*) FROM hooks WHERE unauth=1"),
            "unresolved": sc["unresolved"],          # same key as index() for consistency
            "unresolved_calls": sc["unresolved"],    # back-compat alias
            "by_language": {r["lang"]: r["n"] for r in langs},
            "by_kind": {r["kind"]: r["n"] for r in kinds},
        }



_JS_FAMILY = {"javascript", "typescript", "tsx"}


def _fam(lang: str) -> str:
    """Resolution namespace for a language. JavaScript/TypeScript/TSX freely import each
    other, so they share ONE bucket - otherwise a `.tsx` caller can't resolve a symbol
    defined in a `.ts`/`.js` file (the whole inter-file graph of an RN/TS app). Other
    languages resolve only within themselves."""
    return "js" if lang in _JS_FAMILY else lang


def _collapse_types(vals: set) -> tuple[str, str] | None:
    """Reduce a var's asserted types to one (cls, kind), or None if it was assigned
    CONFLICTING classes (untrustworthy - resolve via the ambiguous pool, never exact).
    A single class wins; a hint + construct of that same class keeps the construct."""
    classes = {c for c, _ in vals}
    if len(classes) != 1:
        return None
    return (next(iter(classes)), "construct" if any(k == "construct" for _, k in vals) else "hint")


def _is_free(c) -> bool:
    """Free-function-like: a real function, or a top-level def in languages that
    model every def as a method (Ruby) - i.e. a method with no enclosing class."""
    return c["kind"] == "function" or (c["kind"] == "method" and not c["container"])


def _is_method(c) -> bool:
    return c["kind"] == "method" and bool(c["container"])


def _module_match(path: str, module: str, lang: str) -> bool:
    """Does `path` define `module`? python: the dotted module maps to a path suffix
    (utils.text -> .../utils/text.py or .../utils/text/__init__.py - anchored on
    component boundaries so text.py at the repo root can't claim utils.text). go:
    the file sits in the package's directory. js family: filename stem matches the
    specifier tail (`import * as util from './util'`)."""
    p = path.replace(os.sep, "/")
    if lang == "python":
        suffix = "/" + module.replace(".", "/")
        base = p[:-3] if p.endswith(".py") else p
        return base.endswith(suffix) or base.endswith(suffix + "/__init__")
    tail = module.rsplit("/", 1)[-1].split(".")[0]
    if not tail:
        return False
    if lang == "go":
        return os.path.basename(os.path.dirname(p)) == tail
    return os.path.basename(p).split(".")[0] == tail


def _pref(pool: list, file: str) -> list:
    same = [c for c in pool if c["file"] == file]
    return same if same else pool


def _conf(chosen: list, base: str) -> str:
    return base if len(chosen) == 1 else "ambiguous"


def _classify(receiver, lang, src_symbol, class_names, assign_scope, assign_file, file,
              bound_scope=frozenset(), bound_file=frozenset(), imports=frozenset()):
    """Return (shape, target_class) from the call's receiver text.

    A name bound in the CURRENT scope shadows a same-named global class (STATIC) and a
    module-level construct (assign_file), so scope-local bindings are consulted FIRST.
    Otherwise `def handler(db)` shadowing a module `db = Database()`, or a local named
    like a class, fabricates a wrong `exact` edge and drops the real caller."""
    recv = (receiver or "").strip()
    if recv == "":
        return "FREE", None
    norm = recv.replace("()", "").rstrip(":")
    if recv.startswith("super") or norm in L.SUPER_RECEIVERS:
        return "SUPER", None
    if norm in L.SELF_RECEIVERS:
        return "SELF", None
    # PHP assign vars are stored '$'-stripped, so strip the PHP receiver to match
    # ($m->send() -> var m). NOT for JS/TS, where `$el` and `el` are distinct names.
    rvar = recv.lstrip("$") if lang == "php" else recv
    # A `Name()` receiver is construction only in Python; elsewhere it's a function call
    # whose return type is unverified - it must not match a same-named class as STATIC.
    is_call_recv = recv.endswith("()") and recv[:-2].isidentifier()

    def _shape_for(hit):
        cls, kind = hit
        if (lang, cls) in class_names:  # proven -> exact-eligible; hint -> inferred-only
            return ("INSTANCE" if kind == "construct" else "INSTANCE_HINT"), cls
        return "INSTANCE", None  # local of an out-of-repo type; NOT a static class call

    # 1. scope-local binding (typed or untyped) - masks a same-named global class/var
    if src_symbol is not None:
        hit = assign_scope.get((src_symbol, rvar))
        if hit is not None:
            return _shape_for(hit)
        if (src_symbol, rvar) in bound_scope:  # locally bound but untyped/conflicting
            return "INSTANCE", None
    # 2. genuine static call to a class (nothing local shadows the name); a `Name()`
    #    function-call receiver in a non-Python language is NOT a static class reference
    if (lang, norm) in class_names and not (is_call_recv and lang not in L.CONSTRUCT_BY_CALL_LANGS):
        return "STATIC", norm
    # 3. module-level (file) binding
    fhit = assign_file.get((file, rvar))
    if fhit is not None:
        return _shape_for(fhit)
    if (file, rvar) in bound_file:
        return "INSTANCE", None
    # 4. imported-module receiver: `import util; util.helper()` binds util to the
    #    MODULE, so the callee resolves against that module file's free functions.
    #    After the binding checks (a local/file rebind masks the import); a
    #    from-import also records the module name, but firing on that receiver
    #    requires code that would NameError at runtime anyway.
    if norm in imports:
        return "MODULE", norm
    return "INSTANCE", None


def _resolve_shape(shape, tclass, file, scv, lang, callee, free_like, method_like, class_like, meth_by_nc,
                   mixins={}):
    """Filter pre-indexed candidates by call shape; return (chosen_rows, confidence).
    Same-class / static / typed-instance lookups are O(1) via meth_by_nc."""
    if shape == "FREE":
        # In Ruby a bare call inside a class can hit a same-class method.
        if lang in L.BARE_SELF_METHOD_LANGS and scv:
            selfm = meth_by_nc.get((lang, callee, scv))
            if selfm:
                return selfm, _conf(selfm, "exact")
            # Ruby MRO looks up mixed-in module methods BEFORE Object's top-level
            # defs, so an include/extend/prepend hit masks a same-named free fn.
            # inferred, never exact: another include or a prepend can reorder lookup.
            mixed = [m for mod in mixins.get((lang, scv), ())
                     for m in meth_by_nc.get((lang, callee, mod), ())]
            if mixed:
                return mixed, _conf(mixed, "inferred")
        # Bare-call candidate pool: free functions, plus a same-named class where the
        # language lets a bare Name() hit a class (BARE_CALL_CLASS_LANGS). With both
        # kinds live the shape proves neither - ambiguous over all, never a
        # picked-first exact that drops the class's caller. A lone class candidate
        # still resolves in any language (constructor / class dependency edge).
        pool = list(free_like)
        if class_like and (lang in L.BARE_CALL_CLASS_LANGS or not free_like):
            pool += class_like
        if pool:
            p = _pref(pool, file)
            return p, _conf(p, "exact")
        return [], None

    if shape == "MODULE":
        # The import proves the receiver IS a module; the callee resolves against
        # free functions DEFINED IN that module's file. python: import + path-suffix
        # match is proof-grade (exact when single). go (package = dir name) and js
        # (alias = filename stem) are conventions -> inferred. No candidate = the
        # module is out-of-repo (stdlib/vendor) -> honest unresolved, method_like
        # is implausible for a module attribute call.
        mods = [c for c in free_like if _module_match(c["file"], tclass, lang)]
        if mods:
            return mods, _conf(mods, "exact" if lang == "python" else "inferred")
        return [], None

    if shape == "SELF":
        if scv:
            selfm = meth_by_nc.get((lang, callee, scv))
            if selfm:
                return selfm, _conf(selfm, "exact")
        if method_like:  # inherited or unknown self -> plausible but unverified
            return method_like, _conf(method_like, "inferred")
        return [], None

    if shape == "SUPER":
        other = [c for c in method_like if c["container"] != scv]
        if other:
            return other, _conf(other, "inferred")
        return [], None

    if shape == "INSTANCE_HINT":
        # receiver type is a DECLARED annotation, not proof - resolve, but never exact.
        if tclass:
            tm = meth_by_nc.get((lang, callee, tclass))
            if tm:
                return tm, _conf(tm, "inferred")
        if method_like:
            return method_like, _conf(method_like, "inferred")
        return [], None

    # STATIC / INSTANCE (proven), possibly with an inferred target class
    if tclass:
        tm = meth_by_nc.get((lang, callee, tclass))
        if tm:
            return tm, _conf(tm, "exact")
    if method_like:
        return method_like, _conf(method_like, "inferred")
    return [], None
