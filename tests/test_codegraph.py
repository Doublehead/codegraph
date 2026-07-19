#!/usr/bin/env python
"""Permanent regression suite for codegraph.

Run:  ~/.local/lib/codegraph/.venv/bin/python tests/test_codegraph.py
(or `pytest` if installed). Exits non-zero on any failure.

Covers the 6-language ground truth (exact edge sets + resolution) plus the three
regressions from the 2026-06-26 audit:
  P1  same method name, different containers - re-point Foo.save -> Bar.save is detected
  P2  hook invoked from a graphless subdir walks up to the governing graph
  P3  Go local type inference: Repo{}, &Repo{}, var v Repo resolve exact vs a rival Other.Save
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codegraph import graph as G  # noqa: E402
from codegraph import indexer as I  # noqa: E402
from codegraph import server as SV  # noqa: E402
from codegraph.store import Store  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _lbl(name, cont):
    return f"{cont}.{name}" if cont else name


def _edges(db):
    s = Store(db)
    try:
        out = {}
        for r in s.db.execute(
            "SELECT e.resolution res, src.name sn, src.container sc, dst.name dn, dst.container dc "
            "FROM edges e LEFT JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst"):
            src = _lbl(r["sn"], r["sc"]) if r["sn"] else "<module>"
            out[(src, _lbl(r["dn"], r["dc"]))] = r["res"]
        unres = s.db.execute("SELECT COUNT(*) FROM unresolved").fetchone()[0]
        return out, unres
    finally:
        s.close()


def _index_copy(name):
    """Copy a fixture dir to a fresh temp dir, force-index it, return (tmp, db)."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    shutil.copytree(os.path.join(FIX, name), tmp, dirs_exist_ok=True)
    db = os.path.join(tmp, ".codegraph", "graph.db")
    I.index(tmp, db, force=True)
    return tmp, db


GROUND_TRUTH = {
    "py": {
        ("save", "write"): "exact", ("load", "save"): "exact", ("boot", "save"): "exact",
        ("handler", "Repo"): "exact", ("handler", "Repo.save"): "exact",
        ("Repo.save", "Repo._write"): "exact", ("Repo._write", "save"): "exact",
        ("Repo.fetch", "Repo.save"): "exact", ("inner", "helper"): "exact",
        ("outer", "inner"): "exact", ("helper", "load"): "exact",
        ("recurse", "recurse"): "exact", ("ping", "pong"): "exact", ("pong", "ping"): "exact",
    },
    "js": {
        ("run", "save"): "exact", ("handle", "run"): "exact", ("save", "write"): "exact",
        ("boot", "save"): "exact", ("Repo.store", "Repo.flush"): "exact",
        ("Repo.flush", "save"): "exact", ("loopA", "loopB"): "exact", ("loopB", "loopA"): "exact",
    },
    "ts": {
        ("Base.greet", "Base.format"): "exact", ("Child.greet", "Base.greet"): "inferred",
        ("topLevel", "helper"): "exact",
    },
    "php": {
        ("save", "write"): "exact", ("Repo.store", "Repo.flush"): "exact",
        ("Repo.flush", "save"): "exact", ("Repo.copy", "Repo.flush2"): "exact",
        ("loopA", "loopB"): "exact", ("loopB", "loopA"): "exact",
        ("Helper.go", "save"): "exact", ("Helper.dup", "Helper.go"): "exact",
    },
    "rb": {
        ("save", "write"): "exact", ("Repo.store", "Repo.flush"): "exact",
        ("Repo.flush", "save"): "exact", ("Repo.bareflush", "Repo.flush"): "exact",
        ("M.helper", "save"): "exact", ("loopa", "loopb"): "exact", ("loopb", "loopa"): "exact",
    },
    "go": {
        ("save", "write"): "exact", ("Repo.Store", "Repo.Flush"): "exact",
        ("Repo.Flush", "save"): "exact", ("loopa", "loopb"): "exact", ("loopb", "loopa"): "exact",
    },
}


def test_ground_truth():
    """Every language fixture resolves to its exact expected edge set, 0 unresolved."""
    for lang, expect in GROUND_TRUTH.items():
        tmp, db = _index_copy(lang)
        try:
            got, unres = _edges(db)
            assert unres == 0, f"{lang}: {unres} unresolved (expected 0)"
            assert got == expect, (
                f"{lang}: edge mismatch"
                f"\n  missing={sorted(set(expect) - set(got))}"
                f"\n  extra={sorted(set(got) - set(expect))}"
                f"\n  mislabel={{{', '.join(f'{k}: {got[k]}!={expect[k]}' for k in set(got) & set(expect) if got[k] != expect[k])}}}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_p1_repoint_same_name_diff_container():
    """Changing x=Foo() to x=Bar() (callee name 'save' unchanged) is reported as a
    re-pointed edge - the bug the original name-only diff could not see."""
    tmp, db = _index_copy("regression")
    try:
        main = os.path.join(tmp, "repoint.py")
        s = Store(db)
        before = G.resolved_out_edges(s, main)
        s.close()
        assert ("consumer", "Foo.save") in before, f"baseline wrong: {sorted(before)}"
        with open(main) as fh:
            src = fh.read()
        with open(main, "w") as fh:
            fh.write(src.replace("x = Foo()", "x = Bar()"))
        I.index(tmp, db)
        s = Store(db)
        after = G.resolved_out_edges(s, main)
        s.close()
        assert ("consumer", "Bar.save") in (after - before), "re-point ADD not detected"
        assert ("consumer", "Foo.save") in (before - after), "re-point REMOVE not detected"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p2_hook_walks_up_from_graphless_subdir():
    """codegraph.hook invoked with a subdir that has no graph walks up to the root."""
    tmp, db = _index_copy("py")
    try:
        sub = os.path.join(tmp, "deep", "sub")
        os.makedirs(sub, exist_ok=True)
        target = os.path.join(tmp, "core.py")
        payload = json.dumps({"tool_input": {"file_path": target}})
        out = subprocess.run(
            [sys.executable, "-m", "codegraph.hook", "pre", sub],
            input=payload, capture_output=True, text=True, timeout=30).stdout
        assert "BLAST RADIUS" in out, f"hook did not walk up; stdout={out!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p3_go_local_type_inference():
    """r:=Repo{}, p:=&Repo{}, var v Repo all resolve r.Save() to Repo.Save EXACT,
    correctly disambiguated from a rival Other.Save."""
    tmp, db = _index_copy("regression")
    try:
        s = Store(db)
        got = {(r["sn"], r["res"]) for r in s.db.execute(
            "SELECT src.name sn, e.resolution res FROM edges e JOIN symbols dst ON dst.id=e.dst "
            "JOIN symbols src ON src.id=e.src WHERE dst.name='Save' AND dst.container='Repo'")}
        has_other = s.db.execute(
            "SELECT 1 FROM symbols WHERE name='Save' AND container='Other'").fetchone()
        s.close()
        assert has_other, "Other.Save missing - disambiguation not actually exercised"
        for caller in ("useShort", "usePointer", "useVar"):
            assert (caller, "exact") in got, f"Go {caller} -> Repo.Save not exact; got={sorted(got)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_callback_to_hook():
    """Each WP listener's callback resolves to the right symbol on the right hook -
    the high-confidence callback->hook direction across all callback forms."""
    tmp, db = _index_copy("wp_hooks")
    try:
        s = Store(db)
        regs = {}
        for r in s.db.execute(
            "SELECT h.hook, sy.name n, sy.container c FROM hooks h "
            "LEFT JOIN symbols sy ON sy.id=h.callback_symbol WHERE h.kind='listen'"):
            cb = (f"{r['c']}.{r['n']}" if r["c"] else r["n"]) if r["n"] else None
            regs.setdefault(cb, set()).add(r["hook"])
        s.close()
        assert "rest_api_init" in regs.get("SB_Plugin.register_routes", set()), "method_self callback"
        assert "the_content" in regs.get("sb_render", set()), "free-function callback"
        assert {"wp_ajax_nopriv_sb_save", "wp_ajax_sb_save"} <= regs.get("SB_Plugin.ajax_save", set())
        assert "sb/v1/save" in regs.get("SB_Plugin.rest_save", set()), "register_rest_route callback"
        assert "sb_custom" in regs.get("SB_Helper.boot", set()), "Class::method static callback"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_entry_points_attack_surface():
    """ajax/ajax_nopriv/rest callbacks are enumerated as entry points, unauth flagged."""
    tmp, db = _index_copy("wp_hooks")
    try:
        s = Store(db)
        eps = {(e["hook"], bool(e["unauth"])) for e in s.entry_points()}
        s.close()
        assert ("wp_ajax_nopriv_sb_save", True) in eps, "nopriv must be UNAUTH"
        assert ("wp_ajax_sb_save", False) in eps, "wp_ajax must be auth"
        assert ("sb/v1/save", True) in eps, "REST with __return_true must be UNAUTH"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_fire_synthesizes_hook_edge():
    """An in-repo do_action('sb_custom') yields a hook-tier edge to its listener."""
    tmp, db = _index_copy("wp_hooks")
    try:
        s = Store(db)
        edges = {(r["sn"], r["dn"], r["dc"]) for r in s.db.execute(
            "SELECT src.name sn, dst.name dn, dst.container dc FROM edges e "
            "JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst WHERE e.resolution='hook'")}
        s.close()
        assert ("fire_custom", "boot", "SB_Helper") in edges, f"do_action edge missing; got {edges}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_honest_blind_spots():
    """Closures and dynamic hook names produce NO resolution and NO false edges."""
    tmp, db = _index_copy("wp_hooks")
    try:
        s = Store(db)
        closure = s.db.execute(
            "SELECT callback_symbol FROM hooks WHERE hook='init' AND cb_kind='closure'").fetchone()
        assert closure is not None and closure["callback_symbol"] is None, "closure must not resolve"
        dyn = s.db.execute(
            "SELECT COUNT(*) n FROM hooks WHERE hook IS NULL AND cb_name='dyn'").fetchone()
        assert dyn["n"] == 1, "dynamic hook name must be recorded NULL, not fabricated"
        n_hook_edges = s.db.execute(
            "SELECT COUNT(*) n FROM edges WHERE resolution='hook'").fetchone()["n"]
        assert n_hook_edges == 1, f"exactly 1 hook edge expected (no false edges); got {n_hook_edges}"
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_adversarial_callback_forms():
    """The 19 confirmed audit regressions: double-quoted strings, ::class, namespaced
    FQNs, leading-backslash calls, typed-var callbacks resolve correctly - and a
    collision (Mailer.send vs Other.send) is NEVER guessed wrong."""
    tmp, db = _index_copy("wp_adversarial")
    try:
        s = Store(db)
        regs = {}
        for r in s.db.execute(
            "SELECT h.hook, sy.name n, sy.container c FROM hooks h "
            "LEFT JOIN symbols sy ON sy.id=h.callback_symbol WHERE h.kind='listen'"):
            cb = (f"{r['c']}.{r['n']}" if r["c"] else r["n"]) if r["n"] else None
            regs[r["hook"]] = cb
        eps = {(e["hook"], bool(e["unauth"])) for e in s.entry_points()}
        s.close()
        # double-quoted name + callback
        assert regs.get("dq_hook") == "dq_free", f"double-quoted: {regs.get('dq_hook')}"
        # self::class / static::class -> enclosing class
        assert regs.get("h_self") == "Plug.shared" and regs.get("h_static") == "Plug.shared"
        # namespaced FQN array + string -> bare-normalized to Mailer.send
        assert regs.get("h_nsarr") == "Mailer.send", f"ns array: {regs.get('h_nsarr')}"
        assert regs.get("h_nsstr") == "Mailer.send", f"ns string: {regs.get('h_nsstr')}"
        # typed local var -> Mailer.send via inference, NOT the colliding Other.send
        assert regs.get("h_typed") == "Mailer.send", f"typed-var must pick Mailer, got {regs.get('h_typed')}"
        # leading-backslash global call still captured
        assert regs.get("h_bslash") == "Plug.shared", f"\\add_action: {regs.get('h_bslash')}"
        # admin_post entry points + auth classification
        assert ("wp_ajax_nopriv_go", True) in eps
        assert ("admin_post_nopriv_go", True) in eps, "admin_post_nopriv must be UNAUTH entry"
        assert ("admin_post_go", False) in eps, "admin_post must be auth entry"
        assert ("v1/pub", True) in eps, "REST \\__return_true must be UNAUTH"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_parse_error_recovery_const_namespace():
    """`const NAMESPACE` makes tree-sitter-php collapse the class into ERROR nodes,
    orphaning its methods. Recovery must restore class membership so the self-call AND
    the hook callback resolve to the RIGHT class, not the same-named decoy method."""
    tmp, db = _index_copy("wp_parse_recovery")
    try:
        s = Store(db)
        # methods regained their container
        conts = {r["name"]: r["container"] for r in s.db.execute(
            "SELECT name, container FROM symbols WHERE name IN ('register_routes','cancel_booking_public','uses_helper')")}
        assert conts.get("register_routes") == "Static_Booking_REST_API", f"container lost: {conts}"
        # the [$this,'cancel_booking_public'] callback resolves to THIS class, not the decoy
        cb = s.db.execute(
            "SELECT sy.container c FROM hooks h JOIN symbols sy ON sy.id=h.callback_symbol "
            "WHERE h.cb_name='cancel_booking_public'").fetchone()
        assert cb and cb["c"] == "Static_Booking_REST_API", f"hook callback mis-resolved: {cb}"
        # the call-graph self-call ($this->cancel_booking_public) also resolves to this class
        edge = s.db.execute(
            "SELECT dst.container c, e.resolution r FROM edges e JOIN symbols dst ON dst.id=e.dst "
            "JOIN symbols src ON src.id=e.src WHERE src.name='uses_helper' AND dst.name='cancel_booking_public'").fetchone()
        assert edge and edge["c"] == "Static_Booking_REST_API", f"self-call mis-resolved: {edge}"
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ns_collision_callback_never_guessed():
    """2026-06-27 audit P1: a namespaced callback (\\B\\Mailer::send) whose bare class
    name (Mailer) collides across namespaces must NOT resolve to the first same-named
    method - it stays unresolved. A unique namespaced class (Notifier) still resolves,
    proving the fix didn't over-correct into refusing everything."""
    tmp, db = _index_copy("wp_ns_collision")
    try:
        s = Store(db)
        regs = {}
        for r in s.db.execute(
            "SELECT h.hook, sy.name n, sy.container c FROM hooks h "
            "LEFT JOIN symbols sy ON sy.id=h.callback_symbol WHERE h.kind='listen'"):
            regs[r["hook"]] = (f"{r['c']}.{r['n']}" if r["c"] else r["n"]) if r["n"] else None
        s.close()
        assert regs.get("collide") is None, f"ambiguous bare collision must not guess, got {regs.get('collide')}"
        assert regs.get("unique") == "Notifier.ping", f"unique container must resolve, got {regs.get('unique')}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recovery_does_not_swallow_top_level_fns():
    """2026-06-27 audit P2: parse-error container recovery is bounded to brace-matched
    class bodies. Methods inside a broken class regain their container; genuine
    top-level functions AFTER the class stay containerless, not Broken.helper."""
    tmp, db = _index_copy("wp_recovery_overreach")
    try:
        s = Store(db)
        syms = {r["name"]: (r["container"], r["kind"]) for r in s.db.execute(
            "SELECT name, container, kind FROM symbols")}
        s.close()
        assert syms.get("inside_method") == ("Broken", "method"), f"in-class method lost container: {syms.get('inside_method')}"
        assert syms.get("top_level_helper") == (None, "function"), f"helper swept into class: {syms.get('top_level_helper')}"
        assert syms.get("top_level_outside") == (None, "function"), f"outside swept into class: {syms.get('top_level_outside')}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_incremental_pass_reports_edges_hook():
    """2026-06-27 audit P3: a no-change incremental index (resolution_counts path) must
    still report edges_hook, matching resolve(), so health/activation output never
    silently drops the hook layer."""
    tmp, db = _index_copy("wp_hooks")  # _index_copy already ran a forced index
    try:
        res = I.index(tmp, db, force=False)  # mtime unchanged -> resolution_counts path
        assert res["reindexed"] == 0 and res["unchanged"] > 0, f"expected a no-change pass: {res}"
        assert "edges_hook" in res, "resolution_counts dropped edges_hook"
        assert res["edges_hook"] == 1, f"wp_hooks has exactly 1 hook edge, got {res['edges_hook']}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_root_guard_refuses_broad_roots():
    """Hardening: indexing the filesystem root, home, or an ancestor of home is a
    footgun (walks an enormous tree). _guard_root rejects them; a real project dir
    is accepted."""
    home = os.path.expanduser("~")
    for bad in ("/", home, os.path.dirname(home)):
        try:
            SV._guard_root(bad)
            assert False, f"_guard_root must reject {bad!r}"
        except ValueError:
            pass
    SV._guard_root(FIX)  # a real project directory must NOT raise


def test_clamp_and_like_escape_units():
    """Hardening: integer query params clamp into range (runaway depth/top guard) and
    LIKE wildcards in a file target are escaped to literals."""
    assert SV._clamp(10 ** 9, 1, 25) == 25
    assert SV._clamp(0, 1, 25) == 1 and SV._clamp(-5, 1, 25) == 1
    assert SV._clamp(7, 1, 25) == 7
    assert SV._clamp("nan", 1, 25) == 1  # non-int -> low bound, never crash
    assert SV._like_escape(r"a_b%c\d") == r"a\_b\%c\\d"


def test_like_escape_blocks_wildcard_overmatch():
    """Hardening: a '_' in a file target must match literally, not as a SQL wildcard.
    The escaped LIKE seeds only a_b.php; the old unescaped form over-matched axb.php."""
    tmp, db = _index_copy("like_escape")
    try:
        s = Store(db)
        esc = [os.path.basename(r["file"]) for r in s.db.execute(
            "SELECT file FROM symbols WHERE file LIKE ? ESCAPE '\\'",
            (f"%{SV._like_escape('a_b.php')}%",)).fetchall()]
        raw = [os.path.basename(r["file"]) for r in s.db.execute(
            "SELECT file FROM symbols WHERE file LIKE ?", ("%a_b.php%",)).fetchall()]
        s.close()
        assert sorted(esc) == ["a_b.php"], f"escaped LIKE must match only a_b.php, got {sorted(esc)}"
        assert "axb.php" in raw, f"sanity: unescaped LIKE should over-match, got {sorted(raw)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_type_hint_resolves_inferred():
    """2026-06-27 Gemini audit #1: injected/declared receiver types resolve via the
    annotation - as `inferred` (a hint is a contract, not proof), with the decoy
    same-named method NOT blended, and an interface hint pointing at the interface,
    never a concrete implementor."""
    tmp, db = _index_copy("type_hints")
    try:
        e, _ = _edges(db)
        assert e.get(("injected", "Request.json")) == "inferred", e.get(("injected", "Request.json"))
        assert ("injected", "Other.json") not in e, "decoy method must not be blended into a hint edge"
        assert e.get(("via_interface", "Iface.go")) == "inferred"
        assert ("via_interface", "ImplA.go") not in e, "interface hint must not fabricate an implementor edge"
        assert e.get(("handler", "Svc.do")) == "inferred"          # TS typed param
        assert ("handler", "Svc2.do") not in e
        assert e.get(("notify", "Mailer.send")) == "inferred"      # PHP typed param
        assert ("notify", "Other2.send") not in e
        assert e.get(("Svc.__construct", "Mailer.send")) == "inferred"  # PHP ctor promotion
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_construct_stays_exact():
    """#1 corollary: proven local construction stays `exact` (a hint must not downgrade
    it). Also covers the PHP/JS `$`-receiver bug the audit surfaced - `$x = new Mailer();
    $x->send()` was silently going ambiguous because the stored var dropped the `$`."""
    tmp, db = _index_copy("type_hints")
    try:
        e, _ = _edges(db)
        assert e.get(("built", "Foo.run")) == "exact", "python local construction must stay exact"
        assert e.get(("make", "Mailer.send")) == "exact", "php $x = new Mailer() must resolve exact, not ambiguous"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_assigns_schema_migration():
    """#1 infra: a graph.db created by the older schema (assigns without `kind`) is
    migrated in place on open, not crashed on."""
    tmp = tempfile.mkdtemp(prefix="cgmig_")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    os.makedirs(os.path.dirname(db))
    con = __import__("sqlite3").connect(db)
    con.execute("CREATE TABLE assigns (file TEXT, lang TEXT, scope_symbol INTEGER, var TEXT, cls TEXT)")
    con.commit(); con.close()
    s = Store(db)
    try:
        cols = {r["name"] for r in s.db.execute("PRAGMA table_info(assigns)")}
        assert "kind" in cols, "migration must add assigns.kind to a legacy schema"
    finally:
        s.close(); shutil.rmtree(tmp, ignore_errors=True)


def test_path_global_shortest():
    """Gemini audit #2: path() must return the globally shortest route over ALL
    src/dst candidate pairs, not the first pair that happens to connect."""
    tmp, db = _index_copy("graph_paths")
    try:
        r = SV.path(root=tmp, src="save", dst="load")
        assert r.get("hops") == 1, f"must return 1-hop Short route, got {r.get('hops')}: {r.get('path')}"
        assert r["path"][0] == "Short.save" and r["path"][-1] == "Short.load", r["path"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_callers_group_by_seed_when_ambiguous():
    """Gemini audit #3: a bare name matching >1 definition returns per-seed groups, not
    a blended caller list - so callers of Config.save aren't mixed with User.save. A
    container-qualified name returns a single flat result."""
    tmp, db = _index_copy("ambig_callers")
    try:
        r = SV.callers(root=tmp, symbol="save")
        assert r.get("ambiguous") is True, "bare ambiguous name must be flagged"
        groups = {g["definition"]["symbol"]: {n["symbol"] for n in g["nodes"]} for g in r["groups"]}
        assert groups.get("Config.save") == {"uses_config"}, groups
        assert groups.get("User.save") == {"uses_user"}, groups
        q = SV.callers(root=tmp, symbol="Config.save")
        assert not q.get("ambiguous"), "qualified name must not be ambiguous"
        assert {n["symbol"] for n in q["nodes"]} == {"uses_config"}, q.get("nodes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blast_radius_flags_ambiguous_seed():
    """#3 corollary: blast_radius discloses when a symbol target matches >1 definition
    (its blast radius is a union); a unique target is not flagged."""
    tmp, db = _index_copy("ambig_callers")
    try:
        assert SV.blast_radius(root=tmp, target="save").get("ambiguous_seed") is True
        assert not SV.blast_radius(root=tmp, target="uses_config").get("ambiguous_seed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_indexer_size_cap():
    """Gemini audit #4: oversized files are skipped before tree-sitter (no session hang),
    reported via skipped_large, and small files still index."""
    tmp = tempfile.mkdtemp(prefix="cgcap_")
    open(os.path.join(tmp, "big.py"), "w").write("def f(): pass\n" * 100)
    open(os.path.join(tmp, "small.py"), "w").write("def g(): pass\n")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    old = I.MAX_FILE_BYTES
    I.MAX_FILE_BYTES = 100
    try:
        res = I.index(tmp, db, force=True)
        assert res["skipped_large"] >= 1, res
        s = Store(db)
        names = {r["name"] for r in s.db.execute("SELECT name FROM symbols")}
        s.close()
        assert "g" in names and "f" not in names, f"big skipped, small kept; got {names}"
    finally:
        I.MAX_FILE_BYTES = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_is_minified_heuristic():
    """#4 corollary: a generated single-line blob is detected; normal multi-line code is not."""
    assert I._is_minified(b"x" * 300_000) is True
    assert I._is_minified(b"x\n" * 300_000) is False
    assert I._is_minified(b"def f(): pass\n") is False


def test_js_dollar_var_not_collided():
    """Adversarial-verify finding: the '$'-strip must be PHP-only. In JS/TS `$el` and
    `el` are distinct identifiers; stripping both to `el` collided same-scope locals and
    fabricated a wrong exact edge while dropping the true caller. Each must resolve to
    its own constructed class."""
    tmp, db = _index_copy("dollar_vars")
    try:
        e, _ = _edges(db)
        assert e.get(("f", "El.m")) == "exact", f"$el must resolve to El.m, got {e.get(('f','El.m'))}"
        assert e.get(("f", "Foo.m")) == "exact", f"el must resolve to Foo.m, got {e.get(('f','Foo.m'))}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_size_skip_drops_stale_facts():
    """Adversarial-verify finding: when a file crosses the size cap as the SOLE change in
    an incremental (force=False) pass, its stale symbols/edges must be dropped AND
    committed - previously the forget_file DELETEs were rolled back, leaving a phantom
    'exact' edge to a definition no longer in source."""
    tmp = tempfile.mkdtemp(prefix="cgstale_")
    open(os.path.join(tmp, "a.py"), "w").write("def foo(): pass\n")
    open(os.path.join(tmp, "b.py"), "w").write("def bar():\n    foo()\n")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    old = I.MAX_FILE_BYTES
    try:
        I.index(tmp, db, force=True)
        I.MAX_FILE_BYTES = 50
        open(os.path.join(tmp, "a.py"), "w").write("def foo(): pass\n" + "# pad pad pad pad\n" * 10)
        res = I.index(tmp, db, force=False)  # a.py is the only change, now over-cap
        assert res["reindexed"] == 0 and res["skipped_large"] >= 1, res
        s = Store(db)
        syms = {r["name"] for r in s.db.execute("SELECT name FROM symbols")}
        edges = {(r["sn"], r["dn"]) for r in s.db.execute(
            "SELECT src.name sn, dst.name dn FROM edges e JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst")}
        s.close()
        assert "foo" not in syms, f"stale symbol must be dropped, got {syms}"
        assert ("bar", "foo") not in edges, "stale exact edge to a vanished definition must be gone"
    finally:
        I.MAX_FILE_BYTES = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_scope_conflict_never_wrong_exact():
    """Adversarial-verify finding: a var assigned CONFLICTING classes - a closure-local
    shadow leaking into its parent, or reassignment to a different type - must NOT yield
    a wrong `exact` edge or drop the real caller. It falls to the honest ambiguous pool
    (edges to all candidates). A control where the closure uses a different name keeps
    the clean inferred hint."""
    tmp, db = _index_copy("scope_leak")
    try:
        e, _ = _edges(db)
        # closure leak: both candidates kept (recall), neither is a wrong exact
        assert ("persist", "Account.save") in e, "real caller (Account.save) must be preserved"
        assert ("persist", "AuditLog.save") in e, "leaked candidate also kept (recall over precision)"
        assert e[("persist", "Account.save")] != "exact", "conflicted var must not resolve exact"
        # control: distinct closure var name -> clean inferred hint, no leak
        assert e.get(("clean", "Account.save")) == "inferred"
        assert ("clean", "AuditLog.save") not in e, "no leak when names differ"
        # python reassignment to a different type -> ambiguous to both, never wrong-exact
        assert ("reassigned", "A.m") in e and ("reassigned", "B.m") in e
        assert e[("reassigned", "A.m")] != "exact" and e[("reassigned", "B.m")] != "exact"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scope_local_binding_masks_globals():
    """2nd adversarial pass (D1/D2/D3): a name bound in a function scope shadows a
    same-named module construct OR a same-named global class. The resolver consults the
    local binding first - never a wrong `exact` to the global, never a dropped caller -
    while a genuine module-global reference still resolves exact."""
    tmp, db = _index_copy("class_shadow")
    try:
        e, _ = _edges(db)
        # D1: untyped param shadows module `db = Database()` -> ambiguous to BOTH, no wrong-exact
        assert e.get(("handler", "Database.query")) == "ambiguous", e.get(("handler", "Database.query"))
        assert ("handler", "FakeDB.query") in e, "real candidate must not be dropped"
        # D1 control: genuine module-global reference still resolves exact
        assert e.get(("use_global", "Database.query")) == "exact"
        # D2: param hint shadowing class `session` -> inferred to the declared type, not STATIC
        assert e.get(("hinted", "DBSession.commit")) == "inferred"
        assert ("hinted", "session.commit") not in e, "must not treat the local as a static class call"
        # D3: local construct shadowing class `config` -> exact to the constructed class
        assert e.get(("constructed", "Other.run")) == "exact"
        assert ("constructed", "config.run") not in e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blast_radius_routes_all_source_extensions():
    """2nd adversarial pass (D4): file-target detection derives from EXT_LANG, so a bare
    .jsx (and .mjs/.cts/…) filename audits the file rather than being mis-routed as a
    symbol name."""
    tmp, db = _index_copy("jsx_target")
    try:
        r = SV.blast_radius(root=tmp, target="Btn.jsx")
        assert "error" not in r, r
        seeds = {s["symbol"] for s in r.get("seed_symbols", [])}
        assert "Btn.render" in seeds, f"`.jsx` must route as a file target, got {seeds}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blast_radius_method_named_like_extension():
    """3rd adversarial pass: a Container.method whose method name equals a language
    token (Runner.go) must resolve as a SYMBOL (callers preserved), not be misrouted to
    a file search. And the file fallback is segment-anchored so `Task.go` can't match an
    unrelated `Task.go_dir/mod.py`."""
    tmp, db = _index_copy("ext_token_method")
    try:
        r = SV.blast_radius(root=tmp, target="Runner.go")
        assert {s["symbol"] for s in r.get("seed_symbols", [])} == {"Runner.go"}, r.get("seed_symbols")
        assert r["impacted_symbol_count"] == 1, "runner_caller must be in the blast radius"
        # path-substring collision: Task.go must NOT pull in Task.go_dir/mod.py
        r2 = SV.blast_radius(root=tmp, target="Task.go")
        seeds2 = {s["symbol"] for s in r2.get("seed_symbols", [])}
        assert seeds2 == {"Task.go"}, f"must resolve the method, not the unrelated dir: {seeds2}"
        assert "unrelated_func" not in seeds2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_construction_semantics_never_wrong_exact():
    """4th adversarial pass (D1-D4): language-specific construction semantics must not
    fabricate a wrong `exact`.
      D1  a generic/unresolvable-typed param still shadows a same-named module construct
      D2  PHP `Make()->build()` is a function call (unverified return), not a static class
      D3  Ruby `User.where(...)` is NOT construction (only `.new` is)
      D4  Python `Service()` where Service is both a class and a free function -> inferred
    """
    tmp, db = _index_copy("construct_semantics")
    try:
        e, _ = _edges(db)
        # D1: generic-typed param shadows module `db` -> ambiguous to both, no wrong-exact
        assert e.get(("handler", "Database.query")) == "ambiguous", e.get(("handler", "Database.query"))
        assert ("handler", "Other.query") in e
        # D1: param name == class with a generic annotation -> not a STATIC wrong-exact
        assert e.get(("collide", "Logger.write")) == "ambiguous"
        assert e.get(("collide", "Real.write")) == "ambiguous"
        # D2: PHP function-call receiver -> ambiguous over all build(), Make.build NOT exact
        assert e.get(("run_it", "Make.build")) == "ambiguous", e.get(("run_it", "Make.build"))
        assert ("run_it", "Other.build") in e and ("run_it", "Thing.build") in e
        # D3: Ruby non-.new is not construction -> ambiguous; .new stays exact
        assert e.get(("non_new", "User.save")) == "ambiguous"
        assert e.get(("non_new", "Account.save")) == "ambiguous"
        assert e.get(("with_new", "User.save")) == "exact"
        # D4: Python class/free-function name collision -> inferred, never exact
        assert e.get(("use_service", "Service.run")) == "inferred"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_python_django_signal_coupling():
    """Python coupling layer (Phase 1: Django signals). `@receiver(sig)` and
    `sig.connect(cb)` register listeners; `sig.send()` fires them - producing hook-tier
    edges (fire's enclosing function -> listener) for coupling the call graph can't see.
    Signals are keyed by name, so a fire only reaches listeners on the SAME signal."""
    tmp, db = _index_copy("py_signals")
    try:
        s = Store(db)
        hook_edges = {(r["sn"], (r["dc"] + "." if r["dc"] else "") + r["dn"]) for r in s.db.execute(
            "SELECT src.name sn, dst.container dc, dst.name dn FROM edges e "
            "JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst WHERE e.resolution='hook'")}
        s.close()
        # post_save fire reaches its listeners: decorator, connect-free, connect-self
        assert ("save_user", "cache_user") in hook_edges, hook_edges
        assert ("save_user", "audit_user") in hook_edges
        assert ("save_user", "Mailer.on_save") in hook_edges
        # custom signal stays separate: order_done fire -> notify only
        assert ("finish_order", "notify") in hook_edges
        assert ("finish_order", "cache_user") not in hook_edges, "signals must not cross-link by name"
        assert ("save_user", "notify") not in hook_edges
        # R1: list-form @receiver listens on BOTH signals
        assert ("save_user", "on_change") in hook_edges, "list @receiver must bind post_save"
        assert ("purge", "on_change") in hook_edges, "list @receiver must bind post_delete"
        # R2: keyword `receiver=` form resolves
        assert ("save_user", "kw_handler") in hook_edges, "connect(receiver=...) must resolve"
        # D1: a non-signal `.connect`/`.send` must NOT fabricate a hook edge
        assert ("do_stuff", "noise") not in hook_edges, "non-signal connect/send must not fabricate an edge"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_python_celery_and_routes():
    """Python coupling Phase 2: Celery `@task` <-> `.delay()/.apply_async()` produces a
    caller->task hook edge (async dispatch the call graph misses), evidence-gated so a
    non-task `.delay()` fabricates nothing; web route decorators + Django `path()` register
    ENTRY POINTS (attack surface) with the view resolved."""
    tmp, db = _index_copy("py_celery_routes")
    try:
        s = Store(db)
        hook_edges = {(r["sn"], r["dn"]) for r in s.db.execute(
            "SELECT src.name sn, dst.name dn FROM edges e JOIN symbols src ON src.id=e.src "
            "JOIN symbols dst ON dst.id=e.dst WHERE e.resolution='hook'")}
        routes = {r["hook"]: r["callback_symbol"] for r in s.db.execute(
            "SELECT hook, callback_symbol FROM hooks WHERE hook_class='route'")}
        s.close()
        # Celery: fires reach their task bodies; a non-task .delay() does not
        assert ("trigger", "email_user") in hook_edges, hook_edges
        assert ("trigger", "sync_data") in hook_edges
        assert ("not_a_task", "email_user") not in hook_edges
        assert not any(src == "not_a_task" for src, _ in hook_edges), "non-task .delay must fabricate nothing"
        # Routes: entry points registered, function views resolved (bare AND attribute form)
        assert {"/health", "/users", "users/", "profile/"} <= set(routes), routes
        assert routes.get("users/") is not None, "django path view (bare) must resolve"
        assert routes.get("profile/") is not None, "django path attribute view (views.show_profile) must resolve"
        assert routes.get("/health") is not None, "fastapi route view must resolve"
        # Cross-mechanism collision: a task and a signal sharing the name `pulse` must NOT cross-fire
        assert ("fire_task", "pulse") in hook_edges, "task fire -> task body"
        assert ("fire_signal", "on_pulse") in hook_edges, "signal fire -> receiver"
        assert ("fire_signal", "pulse") not in hook_edges, "signal send must not reach the same-named task"
        assert ("fire_task", "on_pulse") not in hook_edges, "task delay must not reach the same-named signal receiver"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_react_native_component_graph():
    """React Native coupling (Shrogue-validated): the three gaps a .tsx app exposed.
      Gap 1  HOC-wrapped components (`const X = React.memo((p)=>{})`) ARE indexed.
      Gap 2  JSX usage `<Component/>` is a caller edge (the component tree is visible).
      Gap 3  cross-file .tsx -> .ts calls resolve (JS/TS/TSX share a resolution family) -
             without this an RN app's whole inter-file graph is invisible."""
    tmp, db = _index_copy("rn_components")
    try:
        e, _ = _edges(db)
        s = Store(db)
        kinds = {r["name"]: r["kind"] for r in s.db.execute(
            "SELECT name, kind FROM symbols WHERE name IN ('DungeonView','GameScreen')")}
        s.close()
        # Gap 1: HOC component registered as a symbol
        assert kinds.get("DungeonView") == "function", f"HOC component must be indexed, got {kinds}"
        # Gap 2: JSX usage -> caller edges
        assert e.get(("App", "GameScreen")) == "exact", "JSX <GameScreen/> must be a caller edge"
        assert e.get(("GameScreen", "DungeonView")) == "exact", "JSX <DungeonView/> must resolve (incl. HOC)"
        # Gap 3: cross-file .tsx -> .ts resolves
        assert e.get(("GameScreen", "apiUrl")) == "exact", "cross-file .tsx->.ts call must resolve"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scope_config_and_backup_ignores():
    """Monorepo-eval #4/#5: built-in backup excludes drop backup dirs/files (never real source),
    and an `include` scope limits indexing to chosen paths so a vendor monorepo is treated
    as external."""
    # realpath: the engine stores canonical paths; macOS mkdtemp returns the /var
    # alias of /private/var, so compare from the same spelling the engine writes.
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="cgscope_"))

    def w(rel, c="<?php\nfunction f(){}\n"):
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(c)

    def indexed():
        s = Store(db)
        out = {os.path.relpath(r["path"], tmp).replace(os.sep, "/") for r in s.db.execute("SELECT path FROM files")}
        s.close()
        return out

    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        w("mu-plugins/api.php")
        w("plugins/woocommerce/wc.php")
        w("plugins/btq-vendor-reg backup/old.php")   # #4: backup dir (space form)
        w("mu-plugins/notes.bak")                     # #4: backup file
        w("src/backup_service.php")                   # real source - must NOT be excluded
        I.index(tmp, db, force=True)
        files = indexed()
        assert not any(" backup/" in f for f in files), f"backup dir must be excluded: {files}"
        assert "mu-plugins/notes.bak" not in files, ".bak must be excluded"
        assert "src/backup_service.php" in files, "real source named *backup* must be kept"
        assert "plugins/woocommerce/wc.php" in files, "no scope -> vendor still indexed"
        # scope to mu-plugins only -> vendor treated as external
        __import__("json").dump({"include": ["mu-plugins/*"]},
                                open(os.path.join(tmp, ".codegraph", "config.json"), "w"))
        I.index(tmp, db, force=True)
        assert indexed() == {"mu-plugins/api.php"}, f"include scope must limit indexing, got {indexed()}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_metric_call_sites_vs_edge_rows():
    """Monorepo-eval #1 (+ adversarial D1): index and stats AGREE on resolution by CALL SITE -
    including the multi-call-per-line case (`foo(bar())`) that a (src,file,line) key would
    collapse. An ambiguous call to N candidates is 1 site / N edge_rows."""
    tmp = tempfile.mkdtemp(prefix="cgmetric_")
    open(os.path.join(tmp, "m.php"), "w").write(
        "<?php\nclass A{function save(){}}\nclass B{function save(){}}\nclass C{function save(){}}\n")
    open(os.path.join(tmp, "s.php"), "w").write("<?php\nfunction run($x){ $x->save(); }\n")
    open(os.path.join(tmp, "chain.php"), "w").write(  # two EXACT calls on ONE line
        "<?php\nfunction bar(){ return 1; }\nfunction foo($x){ return $x; }\nfunction caller(){ return foo(bar()); }\n")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        res = I.index(tmp, db, force=True)
        st = SV.stats(root=tmp)
        for k in ("edges_exact", "edges_inferred", "edges_ambiguous", "unresolved", "edge_rows"):
            assert res[k] == st[k], f"index/stats disagree on {k}: {res[k]} vs {st[k]}"
        assert res["edges_exact"] == 2, f"two exact call sites on one line -> 2, got {res['edges_exact']}"
        assert res["edges_ambiguous"] == 1, res["edges_ambiguous"]      # one ambiguous call site
        assert res["edge_rows"] >= 2 + 3, res["edge_rows"]              # 2 exact + 3 fanned candidates
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scope_malformed_config_fails_safe():
    """Adversarial D2/D3: a malformed/wrong-typed .codegraph/config.json must never crash
    indexing or the hook - it falls back to indexing everything (no scope)."""
    for bad in ("[]", '"src"', "123", "null", '{"include": 123}', '{"include": "src"}', "not json{"):
        tmp = tempfile.mkdtemp(prefix="cgbadcfg_")
        os.makedirs(os.path.join(tmp, ".codegraph"))
        os.makedirs(os.path.join(tmp, "src"))
        open(os.path.join(tmp, "src", "a.php"), "w").write("<?php\nfunction f(){}\n")
        open(os.path.join(tmp, ".codegraph", "config.json"), "w").write(bad)
        db = os.path.join(tmp, ".codegraph", "graph.db")
        try:
            I.index(tmp, db, force=True)  # must not raise
            s = Store(db)
            n = s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            s.close()
            assert n == 1, f"malformed config {bad!r} must fail safe (index all), got {n} files"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_agent_driven_auto_scope():
    """The agent (not the human) scopes a vendor monorepo: index() surfaces a
    scope_suggestion, suggest_scope excludes only confirmed vendor (WP core + known
    plugins/parent themes) and never the user's code, and the scope() tool applies it -
    writing the config + reindexing so only custom code remains."""
    tmp = tempfile.mkdtemp(prefix="cgauto_")

    def w(p):
        fp = os.path.join(tmp, p)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        open(fp, "w").write("<?php\nfunction f(){}\n")

    w("wp-config.php")
    w("wp-includes/load.php")
    w("wp-content/plugins/woocommerce/wc.php")     # vendor
    w("wp-content/plugins/dokan/dokan.php")         # vendor
    w("wp-content/plugins/btq-app-api/routes.php")  # custom
    w("wp-content/themes/stockie/style.php")        # parent theme = vendor
    w("wp-content/themes/stockie-child/functions.php")  # custom
    w("wp-content/mu-plugins/loader.php")           # custom
    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        sug = I.suggest_scope(tmp)
        ex = sug["suggested_exclude"]
        assert any("woocommerce" in g for g in ex) and any("dokan" in g for g in ex)
        assert any("wp-includes" in g for g in ex)
        assert any("themes/stockie/" in g for g in ex), "parent theme excluded"
        assert not any("btq-app-api" in g or "stockie-child" in g or "mu-plugins" in g for g in ex), \
            "must never suggest excluding the user's own code"
        # index() surfaces the directive on a large unscoped repo
        old = I.SCOPE_SUGGEST_MIN_FILES
        I.SCOPE_SUGGEST_MIN_FILES = 0
        try:
            assert "scope_suggestion" in I.index(tmp, db, force=True)
        finally:
            I.SCOPE_SUGGEST_MIN_FILES = old
        # apply via the scope tool -> only custom code remains
        SV.scope(root=tmp, exclude=ex)
        s = Store(db)
        files = {os.path.relpath(r["path"], tmp).replace(os.sep, "/") for r in s.db.execute("SELECT path FROM files")}
        s.close()
        assert not any(x in f for f in files for x in
                       ("woocommerce", "dokan", "wp-includes", "themes/stockie/")), files
        for keep in ("btq-app-api", "stockie-child", "mu-plugins"):
            assert any(keep in f for f in files), f"{keep} must stay indexed: {files}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hotspots_flags_core_shadow():
    """Monorepo-eval #3 (core-shadow): a vendor shim re-declaring a core function (`__`) makes every
    call bind to it -> a fake mega-hotspot scope alone can't fix. hotspots must FLAG it as a
    suspected_shadow (defined once, called across the repo) and name the file to exclude - a
    moderately-called real symbol is NOT flagged."""
    tmp = tempfile.mkdtemp(prefix="cgshadow_")
    open(os.path.join(tmp, "shim.php"), "w").write("<?php\nfunction __($x){return $x;}\nfunction helper(){}\n")
    for n in range(60):
        open(os.path.join(tmp, f"f{n}.php"), "w").write(f'<?php\nfunction c{n}(){{ __("a"); __("b"); helper(); }}\n')
    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        I.index(tmp, db, force=True)
        h = SV.hotspots(root=tmp, top=10)
        flags = {e["symbol"]: e["suspected_shadow"] for e in h["most_depended_upon"]}
        assert flags.get("__") is True, f"ubiquitous re-declared core fn must be flagged: {flags}"
        assert flags.get("helper") is False, f"moderately-called real symbol must NOT be flagged: {flags}"
        assert "suspected_shadow_note" in h and "shim.php" in h["suspected_shadow_note"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_always_reports_scope():
    """User feedback: 'I never see anything about scoping.' Every index must report scope
    state - a small unscoped repo says so explicitly (not silence); a scoped repo confirms it."""
    tmp = tempfile.mkdtemp(prefix="cgscopestate_")
    open(os.path.join(tmp, "a.php"), "w").write("<?php\nfunction f(){}\n")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        sc = I.index(tmp, db, force=True)["scope"]
        assert sc["applied"] is False and "note" in sc, sc
        os.makedirs(os.path.join(tmp, ".codegraph"), exist_ok=True)
        __import__("json").dump({"exclude": ["vendor/*"]}, open(os.path.join(tmp, ".codegraph", "config.json"), "w"))
        sc = I.index(tmp, db, force=True)["scope"]
        assert sc["applied"] is True and sc["exclude"] == ["vendor/*"], sc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ts_files_no_jsx_warnings():
    """Real Shrogue report: JSX queries were run against plain .ts files, whose `typescript`
    grammar has no JSX nodes -> 'Invalid node type' warning per file. JSX queries must run
    only on JSX-capable grammars (tsx/js); a plain-.ts index is warning-free. HOC detection
    (memo(...)) still runs for .ts."""
    tmp = tempfile.mkdtemp(prefix="cgts_")
    open(os.path.join(tmp, "a.ts"), "w").write("export function f(){ return 1; }\nexport const g = memo(()=>1);\n")
    open(os.path.join(tmp, "b.ts"), "w").write("export class C { m(){} }\n")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    try:
        res = I.index(tmp, db, force=True)
        assert res["warning_count"] == 0, f"plain .ts index must be warning-free, got {res['warnings']}"
        s = Store(db)
        names = {r["name"] for r in s.db.execute("SELECT name FROM symbols")}
        s.close()
        assert "g" in names, "HOC const (memo) still indexed for .ts"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_schema_migration_adds_missing_columns():
    """Real PrintCad bug: a graph.db built before hooks.cb_recv existed crashed indexing
    with 'no column named cb_recv'. Opening with the current engine must comprehensively
    migrate ANY missing inserted column, then index without crashing."""
    sqlite3 = __import__("sqlite3")
    tmp = tempfile.mkdtemp(prefix="cgmig_")
    db = os.path.join(tmp, ".codegraph", "graph.db")
    os.makedirs(os.path.dirname(db))
    con = sqlite3.connect(db)  # OLD-schema hooks table: missing cb_recv (added later)
    con.execute("CREATE TABLE hooks (id INTEGER PRIMARY KEY, file TEXT, lang TEXT, kind TEXT, hook TEXT, "
                "hook_class TEXT, enclosing_symbol INTEGER, cb_kind TEXT, cb_name TEXT, cb_container TEXT, "
                "cb_raw TEXT, callback_symbol INTEGER, entry_point INTEGER, unauth INTEGER, note TEXT, line INTEGER)")
    con.commit()
    con.close()
    open(os.path.join(tmp, "wp.php"), "w").write(
        "<?php\nadd_action('init', 'my_handler');\nfunction my_handler(){}\nfunction fire(){ do_action('init'); }\n")
    try:
        s = Store(db)  # _migrate must add cb_recv (and set the schema version)
        cols = {r["name"] for r in s.db.execute("PRAGMA table_info(hooks)")}
        assert "cb_recv" in cols, f"migration must add hooks.cb_recv, got {cols}"
        s.close()
        res = I.index(tmp, db, force=True)  # must NOT raise 'no column named cb_recv'
        assert "edges_exact" in res, res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_safe_translates_lock():
    """Gemini audit #5: a contended-writer 'database is locked' becomes a polite
    retryable result; any other OperationalError still propagates (no error hiding)."""
    sqlite3 = __import__("sqlite3")
    real = I.index

    def locked(*a, **k): raise sqlite3.OperationalError("database is locked")
    def other(*a, **k): raise sqlite3.OperationalError("no such table: foo")
    try:
        I.index = locked
        res = I.index_safe("/tmp/cg_x", "/tmp/cg_x/.codegraph/graph.db")
        assert res.get("locked") is True and "error" in res, res
        I.index = other
        raised = False
        try:
            I.index_safe("/tmp/cg_x", "/tmp/cg_y")
        except sqlite3.OperationalError:
            raised = True
        assert raised, "non-lock OperationalError must propagate, not be swallowed"
    finally:
        I.index = real


def test_free_call_class_collision_ambiguous():
    """Audit 2026-06-30 P1: a bare Name() where the repo has BOTH a free function and
    a same-named class (different files) proves neither - ambiguous over both, never a
    picked-first exact that drops the class's caller. PHP is the control: functions and
    classes occupy separate namespaces there, so the free function stays exact."""
    tmp, db = _index_copy("free_class_collision")
    try:
        s = Store(db)
        try:
            rows = s.db.execute(
                "SELECT src.name sn, dst.name dn, dst.kind dk, e.resolution res "
                "FROM edges e JOIN symbols src ON src.id=e.src "
                "JOIN symbols dst ON dst.id=e.dst").fetchall()
        finally:
            s.close()
        got = {(r["sn"], r["dn"], r["dk"]): r["res"] for r in rows}
        # python: Name() may call the free fn OR construct the class -> both, ambiguous
        assert got.get(("build", "Report", "function")) == "ambiguous", got
        assert got.get(("build", "Report", "class")) == "ambiguous", "class candidate dropped"
        # js family: legacy `function Modal` vs `class Modal` -> ambiguous over both
        assert got.get(("mount", "Modal", "function")) == "ambiguous", got
        assert got.get(("mount", "Modal", "class")) == "ambiguous", got
        # php control: bare call provably targets the function -> exact, no class edge
        assert got.get(("render", "Widget", "function")) == "exact", got
        assert ("render", "Widget", "class") not in got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_callee_local_binding_masks_global_fn():
    """Audit 2026-06-30 P1: a bare call through a name bound LOCALLY (param, local
    var) targets a runtime value - never a wrong exact to another file's same-named
    function; disclosed unresolved. Carve-outs that must KEEP resolving: nested def /
    const-arrow (the binding IS the same-file definition) and PHP ($-sigiled vars
    can't shadow functions)."""
    tmp, db = _index_copy("callee_shadow")
    try:
        e, _ = _edges(db)
        s = Store(db)
        try:
            unres = {r["callee"] for r in s.db.execute("SELECT callee FROM unresolved")}
        finally:
            s.close()
        assert ("run", "handler") not in e, "param-shadowed callee must not edge to global"
        assert "handler" in unres, "masked call must be disclosed unresolved"
        assert ("rebound", "notify") not in e and "notify" in unres, \
            "locally rebound name masks the cross-file fn"
        assert e.get(("nested_case", "inner")) == "exact", "nested def must keep resolving"
        assert e.get(("jrun", "jhelp")) == "exact", "js const-arrow must keep resolving"
        assert e.get(("prun", "phandler")) == "exact", "php exempt: $param can't shadow fn"
        # ruby: `report = 5; report` is a variable READ - the statement-position
        # identifier capture must not fabricate a call edge (audit parser P1)
        assert ("use_var", "report") not in e, "ruby local var read fabricated a call"
        assert e.get(("real_call", "report")) == "exact", "genuine ruby bare call kept"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ruby_mixin_masks_toplevel_fn():
    """Audit 2026-06-30 P1: Ruby MRO - a bare call inside a class that `include`s a
    module dispatches to the module's method BEFORE any same-named top-level def.
    Resolves inferred to the mixin (never a wrong exact to the free fn, never a
    dropped Util#helper caller). Controls: no include -> free fn stays exact; include
    without the method -> falls through to the free fn."""
    tmp, db = _index_copy("rb_mixin")
    try:
        e, _ = _edges(db)
        assert e.get(("Worker.run", "Util.helper")) == "inferred", e
        assert ("Worker.run", "helper") not in e, "top-level fn is masked by the mixin"
        assert e.get(("Plain.go", "helper")) == "exact", "no include -> free fn is the target"
        assert e.get(("Fallback.go2", "other_fn")) == "exact", "mixin without the method falls through"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_module_qualified_calls_resolve():
    """Audit 2026-06-30 P1: `import util; util.helper()` - Python's dominant
    cross-module call shape - must resolve (import + path-suffix match is
    proof-grade), not land unresolved with callers() blind to it. Suffix anchoring:
    `pkg.text.render()` binds pkg/text.py, never the same-named root text.py decoy.
    Go package-qualified calls resolve inferred (package name = dir is convention).
    Out-of-repo modules (os) stay honestly unresolved."""
    tmp, db = _index_copy("module_qualified")
    try:
        s = Store(db)
        try:
            rows = s.db.execute(
                "SELECT src.name sn, dst.name dn, dst.file df, e.resolution res "
                "FROM edges e JOIN symbols src ON src.id=e.src "
                "JOIN symbols dst ON dst.id=e.dst").fetchall()
            unres = {(r["receiver"], r["callee"]) for r in
                     s.db.execute("SELECT receiver,callee FROM unresolved")}
        finally:
            s.close()
        got = {(r["sn"], r["dn"]): (r["res"], r["df"]) for r in rows}
        res, df = got.get(("main", "helper"), (None, ""))
        assert res == "exact" and df.endswith("util.py"), got
        res, df = got.get(("main", "render"), (None, ""))
        assert res == "exact", "pkg.text.render must resolve exact"
        assert df.replace(os.sep, "/").endswith("pkg/text.py"), \
            f"must bind pkg/text.py, not the root decoy: {df}"
        res, df = got.get(("run", "Save"), (None, ""))
        assert res == "inferred" and df.replace(os.sep, "/").endswith("store/store.go"), got
        assert ("os", "getcwd") in unres, "out-of-repo module stays disclosed unresolved"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_js_generators_extracted():
    """Audit 2026-06-30 P1: `function* gen()` and `const p = function* () {}` must be
    definitions - otherwise calls inside them get NULL src and calls TO them drop."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "g.js"), "w") as f:
            f.write("function helper() { return 1; }\n"
                    "function* walkTree() {\n  helper();\n  yield 1;\n}\n"
                    "const pager = function* () { helper(); };\n"
                    "function drive() {\n  walkTree();\n}\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        e, _ = _edges(db)
        assert e.get(("walkTree", "helper")) == "exact", "call INSIDE generator dropped"
        assert e.get(("drive", "walkTree")) == "exact", "call TO generator dropped"
        assert e.get(("pager", "helper")) == "exact", "generator expression not a def"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_git_discovery_unicode_and_embedded_repos():
    """Audit 2026-06-30 P1 x2 (dropped callers via discovery): (a) git's default
    quotepath octal-quotes non-ASCII filenames into dead paths after join - the file
    silently vanishes; (b) an embedded git repo / submodule shows in ls-files as a
    bare directory entry whose contents are never listed - every caller inside is
    invisible. Both must index."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        sub = os.path.join(tmp, "sub")
        os.makedirs(sub)
        for d in (tmp, sub):
            subprocess.run(["git", "-C", d, "init", "-q"], check=True,
                           capture_output=True, timeout=30)
        with open(os.path.join(tmp, "top.py"), "w") as f:
            f.write("def topfn():\n    pass\n")
        with open(os.path.join(tmp, "café.py"), "w", encoding="utf-8") as f:
            f.write("def caffn():\n    pass\n")
        with open(os.path.join(sub, "inner.py"), "w") as f:
            f.write("def nestedfn():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        s = Store(db)
        try:
            names = {r["name"] for r in s.db.execute("SELECT name FROM symbols")}
        finally:
            s.close()
        assert "topfn" in names
        assert "caffn" in names, "quotepath-mangled unicode filename dropped"
        assert "nestedfn" in names, "embedded git repo contents invisible"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_crash_window_forces_reresolve():
    """Audit 2026-06-30 P1: a kill between per-file commits and resolve()'s edge
    rebuild leaves committed file facts with stale edges that a changed-count gate
    trusts forever (and recycled rowids can turn into wrong exacts). The persisted
    resolve_pending flag must survive the crash and force the next index to
    re-resolve even with zero changed files."""
    from codegraph import parser as P
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        a, b = os.path.join(tmp, "a.py"), os.path.join(tmp, "b.py")
        with open(b, "w") as f:
            f.write("def two():\n    pass\ndef three():\n    pass\n")
        with open(a, "w") as f:
            f.write("def one():\n    two()\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        e, _ = _edges(db)
        assert e.get(("one", "two")) == "exact"
        # simulate the crash window: file fact committed, process dies pre-resolve
        with open(a, "w") as f:
            f.write("def one():\n    three()\n")
        s = Store(db)
        try:
            s.mark_resolve_pending()
            with open(a, "rb") as f:
                data = f.read()
            import hashlib
            s.index_file(a, hashlib.sha1(data).hexdigest(), os.stat(a).st_mtime,
                         0.0, P.parse(data, "python"))
            assert s.resolve_pending(), "flag must survive the simulated kill"
        finally:
            s.close()
        # incremental run with ZERO changed files must still re-resolve
        I.index(tmp, db)
        e, _ = _edges(db)
        assert e.get(("one", "three")) == "exact", "stale edges trusted after crash"
        assert ("one", "two") not in e
        s = Store(db)
        try:
            assert not s.resolve_pending(), "flag must clear with resolve()'s commit"
        finally:
            s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cycles_exclusions_and_self_loops():
    """Teeth gap (audit 2026-06-30): cycles() had zero coverage. Contract: exact SCCs
    and genuine self-loops ARE reported; inferred and hook edges are EXCLUDED (a
    reported circular dependency must be trustworthy)."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        db = os.path.join(tmp, ".codegraph", "graph.db")
        st = Store(db)
        try:
            ids = {}
            for n in ("a", "b", "rec", "i1", "i2", "h1", "h2"):
                cur = st.db.execute(
                    "INSERT INTO symbols(file,name,kind,container,lang,start_line,end_line,start_byte,signature)"
                    " VALUES('f.py',?, 'function',NULL,'python',1,1,0,'')", (n,))
                ids[n] = cur.lastrowid
            rows = [(ids["a"], ids["b"], "exact"), (ids["b"], ids["a"], "exact"),
                    (ids["rec"], ids["rec"], "exact"),
                    (ids["i1"], ids["i2"], "inferred"), (ids["i2"], ids["i1"], "inferred"),
                    (ids["h1"], ids["h2"], "hook"), (ids["h2"], ids["h1"], "hook")]
            st.db.executemany(
                "INSERT INTO edges(src,dst,kind,resolution,file,line,ref_id) VALUES(?,?,'calls',?,'f.py',1,NULL)",
                rows)
            st.db.commit()
            comps = G.cycles(st, min_size=2)
        finally:
            st.close()
        flat = sorted(tuple(sorted(c)) for c in comps)
        assert (ids["a"], ids["b"]) in flat, "exact mutual recursion must be reported"
        assert (ids["rec"],) in flat, "exact self-loop must be reported"
        assert not any(ids["i1"] in c for c in comps), "inferred cycle must be excluded"
        assert not any(ids["h1"] in c for c in comps), "hook cycle must be excluded"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_forget_file_purges_every_table():
    """Teeth gap: forget_file must purge assigns + hooks too - stale type bindings
    corrupt later resolution; stale hooks duplicate listener output."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        f = os.path.join(tmp, "p.php")
        with open(f, "w") as fh:
            fh.write("<?php\nclass Foo { public function m() {} }\n"
                     "function cb() { $x = new Foo(); $x->m(); }\n"
                     "add_action('init', 'cb');\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            fp = st.db.execute("SELECT path FROM files").fetchone()["path"]
            pre = {t: st.db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                   for t in ("symbols", "refs", "assigns", "hooks", "imports", "files")}
            assert pre["assigns"] > 0 and pre["hooks"] > 0, f"fixture must populate tables: {pre}"
            st.forget_file(fp)
            st.db.commit()
            for t in ("symbols", "refs", "assigns", "hooks", "imports", "files"):
                left = st.db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                assert left == 0, f"forget_file left {left} rows in {t}"
        finally:
            st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pref_same_file_free_fn_wins():
    """Teeth gap: _pref - a bare call with a same-file def and a rival cross-file def
    resolves EXACT to the same-file one (single edge), not ambiguous across both."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "a.py"), "w") as f:
            f.write("def helper():\n    pass\n\ndef caller():\n    helper()\n")
        with open(os.path.join(tmp, "b.py"), "w") as f:
            f.write("def helper():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            rows = st.db.execute(
                "SELECT dst.file df, e.resolution res FROM edges e "
                "JOIN symbols src ON src.id=e.src JOIN symbols dst ON dst.id=e.dst "
                "WHERE src.name='caller'").fetchall()
        finally:
            st.close()
        assert len(rows) == 1 and rows[0]["res"] == "exact", [dict(r) for r in rows]
        assert rows[0]["df"].endswith("a.py"), "same-file def must win"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_home_graph_never_governs():
    """Audit 2026-06-30 P2: a stray $HOME/.codegraph/graph.db must NOT capture every
    graphless project below it; a real project graph under home still governs."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="cgtest_"))
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = tmp
        os.makedirs(os.path.join(tmp, ".codegraph"))
        open(os.path.join(tmp, ".codegraph", "graph.db"), "w").close()  # the stray
        proj = os.path.join(tmp, "proj")
        sub = os.path.join(proj, "sub")
        os.makedirs(sub)
        assert I.find_graph_root(sub) is None, "stray home graph captured a graphless project"
        os.makedirs(os.path.join(proj, ".codegraph"))
        open(os.path.join(proj, ".codegraph", "graph.db"), "w").close()
        assert I.find_graph_root(sub) == os.path.realpath(proj), "real project graph must govern"
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_db_quarantined_and_rebuilt():
    """Audit 2026-06-30 P2: a corrupt graph.db (truncated mid-write) must be
    quarantined + rebuilt by index() - DISCLOSED - instead of raising
    sqlite3.DatabaseError on every tool/hook run forever."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "a.py"), "w") as f:
            f.write("def fn():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        with open(db, "wb") as f:
            f.write(b"this is not a sqlite database, it is garbage" * 100)
        res = I.index(tmp, db)
        assert "healed" in res, f"corruption must be disclosed+healed: {res}"
        assert os.path.exists(db + ".corrupt"), "corrupt DB must be quarantined, not destroyed"
        st = Store(db)
        try:
            names = {r["name"] for r in st.db.execute("SELECT name FROM symbols")}
        finally:
            st.close()
        assert "fn" in names, "graph must be rebuilt from source after healing"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mtime_tick_change_not_skipped():
    """Audit 2026-06-30 P3: a content change landing in the same mtime tick was
    skipped stale forever. The fast path now requires mtime AND size to match."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        f = os.path.join(tmp, "a.py")
        with open(f, "w") as fh:
            fh.write("def first():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        mt = os.stat(f).st_mtime
        with open(f, "w") as fh:
            fh.write("def second_longer_name():\n    pass\n")
        os.utime(f, (mt, mt))  # same tick, different content+size
        I.index(tmp, db)
        st = Store(db)
        try:
            names = {r["name"] for r in st.db.execute("SELECT name FROM symbols")}
        finally:
            st.close()
        assert "second_longer_name" in names, "same-tick content change skipped stale"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_changed_files_skips_uningestible():
    """Audit 2026-06-30 P3: size-capped / minified files were reported 'new' by
    pending() on every call forever - index() refuses them, so they are
    uningestible, not pending."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "ok.py"), "w") as f:
            f.write("def fn():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        with open(os.path.join(tmp, "huge.js"), "w") as f:
            f.write("x" * (I.MAX_FILE_BYTES + 10))
        with open(os.path.join(tmp, "mini.js"), "w") as f:
            f.write("var a=1;" * 40000)  # 320KB single line -> minified heuristic
        changed = I.changed_files(tmp, db)
        names = {os.path.basename(p) for p, _ in changed}
        assert "huge.js" not in names, "size-capped file perpetually 'new'"
        assert "mini.js" not in names, "minified file perpetually 'new'"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hotspots_counts_call_sites_and_dependents():
    """Audit 2026-06-30 P3: hotspots fan-in counted edge ROWS, so one ambiguous call
    fanned to N candidates inflated N fan-ins from a single site. Now: fan_in =
    distinct call sites, plus `dependents` = distinct calling symbols."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "m.py"), "w") as f:
            f.write("def target():\n    pass\n\n"
                    "def spam():\n    target()\n    target()\n    target()\n\n"
                    "def once():\n    target()\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            hs = G.hotspots(st, top=5)
        finally:
            st.close()
        t = next(e for e in hs["most_depended_upon"] if e["symbol"] == "target")
        assert t["fan_in"] == 4, f"4 call sites expected: {t}"
        assert t["dependents"] == 2, f"2 distinct callers expected: {t}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wp_callback_multi_free_never_guessed():
    """Audit 2026-06-30 P2: two same-named free callbacks in different files - a WP
    registration must not link the first one found. Same-file registration wins;
    a third-file registration with no tiebreak resolves to NOTHING."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "one.php"), "w") as f:
            f.write("<?php\nfunction dup_cb() {}\nadd_action('init', 'dup_cb');\n")
        with open(os.path.join(tmp, "two.php"), "w") as f:
            f.write("<?php\nfunction dup_cb() {}\n")
        with open(os.path.join(tmp, "three.php"), "w") as f:
            f.write("<?php\nadd_action('boot', 'dup_cb');\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            rows = st.db.execute(
                "SELECT h.file hf, h.callback_symbol cs, s.file sf FROM hooks h "
                "LEFT JOIN symbols s ON s.id=h.callback_symbol WHERE h.kind='listen'").fetchall()
        finally:
            st.close()
        by_reg = {os.path.basename(r["hf"]): r for r in rows}
        assert by_reg["one.php"]["cs"] is not None \
            and by_reg["one.php"]["sf"].endswith("one.php"), "same-file registration must win"
        assert by_reg["three.php"]["cs"] is None, \
            "ambiguous cross-file callback must resolve to NOTHING, never a guess"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_braceless_recovery_not_swept():
    """Audit 2026-06-30 P2: brace-based container recovery ran on brace-less
    languages - a dict literal after a broken Python class swept genuine top-level
    functions into the class as phantom methods. Recovery is brace-langs only."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "broken.py"), "w") as f:
            f.write("class Broken(:\n\ndef standalone():\n    pass\n\nX = {'a': 1}\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            row = st.db.execute("SELECT container, kind FROM symbols WHERE name='standalone'").fetchone()
        finally:
            st.close()
        if row is not None:  # parse recovery may or may not surface the def at all
            assert row["container"] is None, "top-level fn swept into broken class"
            assert row["kind"] == "function", row["kind"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hoc_member_call_no_phantom():
    """Audit 2026-06-30 P3: `const rows = items.map(fn)` fabricated a phantom symbol
    per data pipeline. Components (Capitalized), hooks (useX) and bare-identifier
    wrappers (memo) still register; member-call data bindings do not."""
    tmp = tempfile.mkdtemp(prefix="cgtest_")
    try:
        with open(os.path.join(tmp, "c.ts"), "w") as f:
            f.write("const rows = items.map((r) => r * 2);\n"
                    "const Memo = React.memo(() => 1);\n"
                    "const useThing = create(() => 2);\n"
                    "const g = memo(() => 1);\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        st = Store(db)
        try:
            names = {r["name"] for r in st.db.execute("SELECT name FROM symbols")}
        finally:
            st.close()
        assert "rows" not in names, "data-pipeline binding registered as phantom symbol"
        assert {"Memo", "useThing", "g"} <= names, names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pending_reports_cross_file_flips():
    """Audit 2026-06-30 P3: pending() diffed only disk-changed files, hiding edge
    flips its own global re-resolution causes elsewhere. A new same-named def must
    surface the OTHER file's exact -> ambiguous flip as state 'reresolved'."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="cgtest_"))
    try:
        with open(os.path.join(tmp, "caller.py"), "w") as f:
            f.write("def run():\n    save()\n")
        with open(os.path.join(tmp, "orig.py"), "w") as f:
            f.write("def save():\n    pass\n")
        db = os.path.join(tmp, ".codegraph", "graph.db")
        I.index(tmp, db, force=True)
        with open(os.path.join(tmp, "rival.py"), "w") as f:
            f.write("def save():\n    pass\n")
        st = Store(db)
        try:
            res = G.pending(st, tmp, db)
        finally:
            st.close()
        by_file = {d["file"]: d for d in res["deltas"]}
        assert "rival.py" in by_file and by_file["rival.py"]["state"] == "new"
        c = by_file.get("caller.py")
        assert c is not None, f"cross-file flip not reported: {res['deltas']}"
        assert c["state"] == "reresolved", c
        assert any("[exact]" in x for x in c["removed"]), c
        assert any("[ambiguous]" in x for x in c["added"]), c
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def test_wp_const_namespace_routes():
    """Pre-existing WP bug: register_rest_route(Foo::NS, '/x') rendered as '?/x'
    because the namespace is a class constant, not a string literal. String consts
    (const NS = '...') and define()s are now captured and swapped into the route
    name - cross-file (the const usually lives in another class) and for self::NS.
    A genuinely dynamic namespace ($ns . '/x') shows its expression, never a bare '?'."""
    tmp, db = _index_copy("wp_const_routes")
    try:
        s2 = Store(db)
        try:
            routes = {r["hook"]: r["unauth"] for r in s2.db.execute(
                "SELECT hook, unauth FROM hooks WHERE hook_class='rest'")}
        finally:
            s2.close()
        assert "zrougable/v1/scene" in routes, ("self::NS must resolve", routes)
        assert "zrougable/v1/voice-token" in routes, ("cross-file Class::NS must resolve", routes)
        assert not any(h.startswith("?") for h in routes), ("no bare '?' route", routes)
        assert not any("::" in h for h in routes), ("no unresolved const token left", routes)
        dyn = [h for h in routes if "$ns" in h]
        assert dyn, ("dynamic route must show its expression, not '?'", routes)
        # pure-listener plugin: edges_hook=0 is expected and the stats note must say so,
        # so a diagnosing agent doesn't misread it as a broken extractor (the window-12 loop)
        st = Store(db)
        try:
            stats = st.stats()
        finally:
            st.close()
        assert stats["edges_hook"] == 0 and stats["hook_registrations"] >= 3
        assert "edges_hook_note" in stats and "EXPECTED" in stats["edges_hook_note"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



TESTS = [
    test_ground_truth,
    test_p1_repoint_same_name_diff_container,
    test_p2_hook_walks_up_from_graphless_subdir,
    test_p3_go_local_type_inference,
    test_wp_callback_to_hook,
    test_wp_entry_points_attack_surface,
    test_wp_fire_synthesizes_hook_edge,
    test_wp_honest_blind_spots,
    test_wp_adversarial_callback_forms,
    test_wp_parse_error_recovery_const_namespace,
    test_wp_const_namespace_routes,
    test_ns_collision_callback_never_guessed,
    test_recovery_does_not_swallow_top_level_fns,
    test_incremental_pass_reports_edges_hook,
    test_root_guard_refuses_broad_roots,
    test_clamp_and_like_escape_units,
    test_like_escape_blocks_wildcard_overmatch,
    test_type_hint_resolves_inferred,
    test_construct_stays_exact,
    test_assigns_schema_migration,
    test_path_global_shortest,
    test_callers_group_by_seed_when_ambiguous,
    test_blast_radius_flags_ambiguous_seed,
    test_indexer_size_cap,
    test_is_minified_heuristic,
    test_js_dollar_var_not_collided,
    test_size_skip_drops_stale_facts,
    test_scope_conflict_never_wrong_exact,
    test_scope_local_binding_masks_globals,
    test_blast_radius_routes_all_source_extensions,
    test_blast_radius_method_named_like_extension,
    test_construction_semantics_never_wrong_exact,
    test_python_django_signal_coupling,
    test_python_celery_and_routes,
    test_react_native_component_graph,
    test_scope_config_and_backup_ignores,
    test_metric_call_sites_vs_edge_rows,
    test_scope_malformed_config_fails_safe,
    test_agent_driven_auto_scope,
    test_hotspots_flags_core_shadow,
    test_index_always_reports_scope,
    test_ts_files_no_jsx_warnings,
    test_schema_migration_adds_missing_columns,
    test_index_safe_translates_lock,
    test_free_call_class_collision_ambiguous,
    test_callee_local_binding_masks_global_fn,
    test_ruby_mixin_masks_toplevel_fn,
    test_module_qualified_calls_resolve,
    test_git_discovery_unicode_and_embedded_repos,
    test_crash_window_forces_reresolve,
    test_js_generators_extracted,
    test_cycles_exclusions_and_self_loops,
    test_forget_file_purges_every_table,
    test_pref_same_file_free_fn_wins,
    test_home_graph_never_governs,
    test_corrupt_db_quarantined_and_rebuilt,
    test_mtime_tick_change_not_skipped,
    test_changed_files_skips_uningestible,
    test_hotspots_counts_call_sites_and_dependents,
    test_wp_callback_multi_free_never_guessed,
    test_braceless_recovery_not_swept,
    test_hoc_member_call_no_phantom,
    test_pending_reports_cross_file_flips,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
