"""Adversarial probe helper: index a single PHP file and dump its hook extraction
+ resolution + synthesized hook edges as JSON.

  ~/.local/lib/codegraph/.venv/bin/python tests/hookcheck.py /path/to/snippet.php
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codegraph import indexer as I  # noqa: E402
from codegraph.store import Store  # noqa: E402

php = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="hookcheck_")
try:
    shutil.copy(php, os.path.join(tmp, os.path.basename(php)))
    db = os.path.join(tmp, ".codegraph", "graph.db")
    I.index(tmp, db, force=True)
    s = Store(db)

    def sym(i):
        if i is None:
            return None
        r = s.symbol_by_id(i)
        if not r:
            return None
        return f"{r['container']}.{r['name']}" if r["container"] else r["name"]

    hooks = [{
        "kind": h["kind"], "hook": h["hook"], "class": h["hook_class"],
        "cb_kind": h["cb_kind"], "cb_name": h["cb_name"], "cb_container": h["cb_container"],
        "resolved_callback": sym(h["callback_symbol"]),
        "entry": bool(h["entry_point"]), "unauth": bool(h["unauth"]), "note": h["note"],
    } for h in s.db.execute("SELECT * FROM hooks ORDER BY line")]
    edges = [{"src": sym(r["src"]), "dst": sym(r["dst"])} for r in s.db.execute(
        "SELECT src,dst FROM edges WHERE resolution='hook'")]
    symbols = [(f"{r['container']}.{r['name']}" if r["container"] else r["name"])
               for r in s.db.execute("SELECT name,container FROM symbols")]
    print(json.dumps({"hooks": hooks, "hook_edges": edges, "symbols": symbols}, indent=1))
    s.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)
