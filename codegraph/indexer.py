"""Walk a repo, parse changed files, keep the graph in sync.

File discovery prefers `git ls-files` (so .gitignore is honoured exactly) and falls
back to a plain walk with a default ignore set when the tree isn't a git repo.
Re-index is incremental: a file is re-parsed only when its content hash changes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import time

from . import languages as L
from . import parser as P
from .store import Store

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
    ".nuxt", "out", "target", "coverage", ".idea", ".vscode", "bower_components",
    ".codegraph",
}

# Cap parse input. tree-sitter has no timeout; a committed multi-MB minified
# bundle (vendor.js, *.min.js) would lock the CPU and hang the whole MCP session.
# Skips are surfaced as warnings, never silent. Override via CODEGRAPH_MAX_FILE_BYTES.
MAX_FILE_BYTES = int(os.environ.get("CODEGRAPH_MAX_FILE_BYTES", 1_500_000))
SCOPE_SUGGEST_MIN_FILES = 1500  # above this, an unscoped repo gets a scope suggestion in index()
# Even under the byte cap, a generated single-line file (few newlines, huge bytes
# per line) parses pathologically slowly - skip those too.
_MINIFIED_MIN_BYTES = 200_000
_MINIFIED_BYTES_PER_LINE = 5_000


def _is_minified(data: bytes) -> bool:
    if len(data) < _MINIFIED_MIN_BYTES:
        return False
    lines = data.count(b"\n") + 1
    return len(data) / lines > _MINIFIED_BYTES_PER_LINE


def find_graph_root(start: str) -> str | None:
    """Nearest ancestor (incl. start) holding a .codegraph/graph.db - git-style.
    Single source of truth for root resolution (MCP server, hooks). The $HOME check
    runs BEFORE the graph check: a stray ~/.codegraph/graph.db must never govern
    (it would silently capture every graphless project); _guard_root refuses $HOME
    as a root for the same reason. realpath so alias spellings converge."""
    d = os.path.realpath(os.path.abspath(start))
    home = os.path.realpath(os.path.expanduser("~"))
    while True:
        if d == home:
            return None
        if os.path.exists(os.path.join(d, ".codegraph", "graph.db")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _git_files(root: str) -> list[str] | None:
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        # core.quotepath=off: git's default octal-quoting ("caf\303\251.py") turns a
        # non-ASCII filename into a dead path after join - the file and every caller
        # in it silently vanish from the graph.
        out = subprocess.run(
            ["git", "-C", root, "-c", "core.quotepath=off",
             "ls-files", "-co", "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        if out.returncode != 0:
            return None
        files = []
        for line in out.stdout.splitlines():
            if not line:
                continue
            p = os.path.join(root, line)
            # A directory entry is a submodule gitlink or an embedded git repo -
            # ls-files never lists their contents, so walk them explicitly or every
            # caller inside is invisible to the graph.
            if os.path.isdir(p):
                files.extend(_walk_files(p))
            else:
                files.append(p)
        return files
    except Exception:
        return None


def _walk_files(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            found.append(os.path.join(dirpath, fn))
    return found


# Always-excluded backup/editor artifacts (matched against the POSIX relpath; fnmatch's
# `*` spans '/'). Delimited so a real source file like `backup_service.php` is NOT dropped
# - only files inside a *backup* directory or with a backup extension. Independent of
# .gitignore (these often aren't git-ignored, e.g. WP "<plugin> backup/" dirs).
DEFAULT_EXCLUDE_GLOBS = (
    "*.bak", "*.orig", "*~", "*.swp",
    "backup/*", "backups/*", "_backup/*", "_backups/*",          # backup dirs at repo root
    "*/backup/*", "*/backups/*", "*/_backup/*", "*/_backups/*",  # backup dirs anywhere
    "* backup/*", "*-backup/*", "*.backup/*", "*_backup/*",
    "*/*wpmc-backups*/*", "*.wpmc-backups/*",
)


# Well-known third-party WordPress plugin/theme directory slugs + WP core dirs. Used ONLY
# to auto-suggest EXCLUDES (never to hide your code): a slug here that's PRESENT in the repo
# is offered for exclusion. Unknown dirs are kept (safe). Coarse + stable (dir-level), unlike
# a brittle function-name denylist. Extend freely.
KNOWN_VENDOR_DIRS = {
    "woocommerce", "woocommerce-subscriptions", "woocommerce-admin", "dokan", "dokan-pro",
    "dokan-lite", "elementor", "elementor-pro", "jetpack", "akismet", "contact-form-7",
    "wordpress-seo", "wpforms", "wpforms-lite", "wordfence", "updraftplus", "w3-total-cache",
    "wp-super-cache", "advanced-custom-fields", "advanced-custom-fields-pro", "wpml",
    "all-in-one-seo-pack", "redux-framework", "wp-rocket", "mailchimp-for-woocommerce",
    "really-simple-ssl", "litespeed-cache", "wp-mail-smtp", "classic-editor", "regenerate-thumbnails",
    # heavy parent themes (a *-child theme is your code; the parent is vendor)
    "storefront", "astra", "stockie", "hello-elementor", "twentytwentyone", "twentytwentytwo",
    "twentytwentythree", "twentytwentyfour",
}
WP_CORE_DIRS = ("wp-admin", "wp-includes")


def suggest_scope(root: str) -> dict:
    """Best-effort, SAFE scope suggestion for a big vendor-heavy repo: a list of EXCLUDE
    globs for confirmed third-party directories that are actually present. Exclude-based on
    purpose - it can never hide your own code (unknown dirs are kept; code you add later
    stays in). Returns {} when nothing worth excluding is found. The agent reviews + applies."""
    exclude: list[str] = []
    reason = ""
    is_wp = any(os.path.exists(os.path.join(root, p)) for p in
                ("wp-config.php", "wp-includes", "wp-content", "wp-load.php"))
    if is_wp:
        for core in WP_CORE_DIRS:
            if os.path.isdir(os.path.join(root, core)):
                exclude.append(f"{core}/*")
                exclude.append(f"*/{core}/*")
        for base in ("wp-content/plugins", "wp-content/themes", "wp-content/mu-plugins"):
            d = os.path.join(root, *base.split("/"))
            if not os.path.isdir(d):
                continue
            for slug in sorted(os.listdir(d)):
                if slug.lower() in KNOWN_VENDOR_DIRS and os.path.isdir(os.path.join(d, slug)):
                    exclude.append(f"{base}/{slug}/*")
        reason = ("WordPress repo. Excluding WP core + recognized vendor plugins/themes that "
                  "are present. Your code (mu-plugins, child themes, custom plugins) stays "
                  "indexed; add more excludes for any vendor plugin not auto-detected.")
    if not exclude:
        return {}
    return {"suggested_exclude": exclude, "reason": reason}


def _scope_globs(cfg, key: str) -> list[str]:
    """Glob list for an include/exclude key - ONLY if it's a JSON list of strings. Anything
    else (a bare string, number, missing) -> [] (fail safe). Guards against a string being
    iterated char-by-char into single-char globs, and against non-iterables."""
    v = cfg.get(key) if isinstance(cfg, dict) else None
    return [g for g in v if isinstance(g, str) and g] if isinstance(v, list) else []


def _load_scope(root: str) -> tuple[list[str], list[str]]:
    """Read <root>/.codegraph/config.json -> (include_globs, exclude_globs). Missing,
    malformed, or wrong-typed config -> ([], []) and index everything (FAIL SAFE - a bad
    config must never crash indexing or the policing hook). If `include` is non-empty, ONLY
    matching files are indexed (scope to your code, treat vendor as external). `exclude`
    ADDS to the built-in backup defaults. Globs are fnmatch-style against the path relative
    to root; `*` spans directories."""
    try:
        with open(os.path.join(root, ".codegraph", "config.json")) as f:
            cfg = json.load(f)
        return _scope_globs(cfg, "include"), _scope_globs(cfg, "exclude")
    except Exception:  # missing / bad JSON / unreadable -> no scope, never raise
        return [], []


def discover(root: str) -> list[str]:
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    files = [f for f in files if L.lang_for_path(f) and os.path.isfile(f)]
    inc, exc = _load_scope(root)
    exc = (*DEFAULT_EXCLUDE_GLOBS, *exc)
    out = []
    for f in files:
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        if inc and not any(fnmatch.fnmatchcase(rel, g) for g in inc):
            continue  # scope-limited: only included paths
        if any(fnmatch.fnmatchcase(rel, g) for g in exc):
            continue
        out.append(f)
    return out


def _quarantine(db_path: str) -> None:
    """Move a corrupt graph.db (+ WAL/SHM) aside as *.corrupt. The graph is a derived
    cache fully regenerable from source, so rebuild - never crash every session on it."""
    for suf in ("", "-wal", "-shm"):
        p = db_path + suf
        if os.path.exists(p):
            try:
                os.replace(p, p + ".corrupt")
            except OSError:
                try:
                    os.remove(p)
                except OSError:
                    pass


def index(root: str, db_path: str, force: bool = False) -> dict:
    try:
        return _index(root, db_path, force)
    except sqlite3.DatabaseError as e:
        if "locked" in str(e).lower():
            raise  # contention, not corruption - index_safe translates it
        # Corrupt DB (truncated mid-write, disk fault). Quarantine + full rebuild,
        # DISCLOSED in the result - otherwise every MCP tool and hook run dies on it
        # forever even though the graph is regenerable in seconds.
        _quarantine(db_path)
        out = _index(root, db_path, force=True)
        out["healed"] = f"corrupt graph.db quarantined to {db_path}.corrupt; rebuilt from source ({e})"
        return out


def _index(root: str, db_path: str, force: bool = False) -> dict:
    # realpath: every writer converges on one canonical spelling, so an alias-spelled
    # root (macOS /tmp symlink) can't fork the graph into duplicate path universes.
    root = os.path.realpath(root)
    store = Store(db_path)
    try:
        now = time.time()
        files = discover(root)
        current = set(files)
        known = store.known_files()

        # Crash-window guard: a kill between per-file commits and resolve()'s edge
        # rebuild leaves committed file facts whose edges were never rebuilt (and may
        # reference recycled symbol rowids - wrong exacts). Persist a resolve-pending
        # flag before the FIRST mutation; resolve() clears it inside its own commit,
        # so a surviving flag forces the re-resolve a changed-count gate would skip.
        was_pending = store.resolve_pending()
        marked = False

        def _mark():
            nonlocal marked
            if not marked:
                store.mark_resolve_pending()
                marked = True

        # drop files that disappeared
        removed = known - current
        for path in removed:
            _mark()
            store.forget_file(path)

        changed = skipped = failed = skipped_large = dropped = 0
        warnings: list[str] = []
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                failed += 1
                continue
            if st.st_size > MAX_FILE_BYTES:  # never feed a multi-MB blob to tree-sitter
                skipped_large += 1
                warnings.append(f"{os.path.relpath(path, root)}: skipped - {st.st_size} bytes > {MAX_FILE_BYTES} cap")
                if path in known:  # grew past the cap -> drop stale facts (forces a re-resolve+commit)
                    _mark()
                    store.forget_file(path); dropped += 1
                continue
            sig = None if force else store.file_sig(path)
            if sig and sig[0] == st.st_mtime and sig[2] in (None, st.st_size):
                skipped += 1  # mtime+size unchanged -> trust, never read the file
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                failed += 1
                continue
            if _is_minified(data):  # generated single-line bundle -> pathological parse
                skipped_large += 1
                warnings.append(f"{os.path.relpath(path, root)}: skipped - looks minified/generated")
                if path in known:
                    _mark()
                    store.forget_file(path); dropped += 1
                continue
            h = _sha(data)
            if sig and sig[1] == h:  # mtime moved but content identical
                store.touch_mtime(path, st.st_mtime, st.st_size)
                skipped += 1
                continue
            lang = L.lang_for_path(path)
            pf = P.parse(data, lang)
            if pf.warnings:
                warnings.extend(f"{os.path.relpath(path, root)}: {w}" for w in pf.warnings)
            _mark()
            store.index_file(path, h, st.st_mtime, now, pf, size=st.st_size)
            changed += 1

        # Global re-resolution only when the symbol set could have moved. `dropped`
        # covers a file that crossed the size cap as the SOLE change - its forgotten
        # facts must be committed + edges rebuilt, not left in an uncommitted txn.
        res = (store.resolve() if (force or changed or removed or dropped or was_pending)
               else store.resolution_counts())
        out = {
            "root": root,
            "discovered": len(files),
            "reindexed": changed,
            "unchanged": skipped,
            "removed": len(removed),
            "unreadable": failed,
            "skipped_large": skipped_large,
            **res,
            "warnings": warnings[:20],
            "warning_count": len(warnings),
        }
        # ALWAYS report scope state so it's never invisible: scoped, or unscoped-and-fine
        # (small repo), or unscoped-and-large (suggest excludes - the agent applies them, the
        # human shouldn't have to). An exclude suggestion can't hide the user's own code.
        if os.path.exists(os.path.join(root, ".codegraph", "config.json")):
            inc, exc = _load_scope(root)
            out["scope"] = {"applied": True, "include": inc, "exclude": exc}
        else:
            sug = suggest_scope(root) if len(files) > SCOPE_SUGGEST_MIN_FILES else {}
            if sug:
                directive = ("Large unscoped repo. Apply scope(root, exclude=<suggested_exclude>) "
                             "(review/extend the list first) so hotspots/blast-radius reflect the "
                             "user's code, not vendor - then re-query.")
                out["scope"] = {"applied": False, "files": len(files), **sug, "directive": directive}
                out["scope_suggestion"] = out["scope"]  # alias the instructions reference
            else:
                out["scope"] = {"applied": False, "files": len(files),
                                "note": "unscoped; small enough that no scoping is needed"}
        return out
    finally:
        store.close()


def index_safe(root: str, db_path: str, force: bool = False) -> dict:
    """index(), but translate a contended-writer SQLite lock into a polite, retryable
    error instead of crashing the calling agent. Two agents working one repo can both
    try to rewrite edges; under WAL the loser waits busy_timeout, then would raise
    'database is locked'. Returns {"locked": True, ...} in that case."""
    try:
        return index(root, db_path, force=force)
    except sqlite3.OperationalError as e:
        if "lock" in str(e).lower():
            return {"error": "graph is being indexed by another process - retry shortly",
                    "locked": True, "root": os.path.abspath(root)}
        raise


def changed_files(root: str, db_path: str) -> list[tuple[str, str]]:
    """Files whose on-disk hash differs from the stored hash (or are new/deleted).
    Returns (path, state) with state in new|modified|deleted. Read-only; powers the
    `pending` edge-delta check without mutating the stored graph."""
    root = os.path.realpath(root)
    store = Store(db_path)
    try:
        current = {f for f in discover(root)}
        known = store.known_files()
        out = []
        for path in sorted(current | known):
            if path not in known:
                # skip files index() itself refuses (size cap / minified) - they are
                # uningestible, not pending, and would be reported 'new' forever
                try:
                    if os.stat(path).st_size > MAX_FILE_BYTES:
                        continue
                    with open(path, "rb") as fh:
                        if _is_minified(fh.read()):
                            continue
                except OSError:
                    continue
                out.append((path, "new"))
            elif path not in current:
                out.append((path, "deleted"))
            else:
                try:
                    with open(path, "rb") as fh:
                        h = _sha(fh.read())
                except OSError:
                    continue
                if h != store.file_hash(path):
                    out.append((path, "modified"))
        return out
    finally:
        store.close()
