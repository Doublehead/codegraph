"""Background watcher: keeps a project's graph live against changes the edit hooks
can't see - your IDE/vim, `git pull/checkout`, Bash mutations.

Dependency-free polling (no fswatch/watchman needed). Each poll is cheap: the
indexer's mtime fast-path means an unchanged tree is just a stat sweep and the
global re-resolve is skipped entirely. One watcher per project (pidfile dedup); it
retires after a stretch of no changes so idle projects don't keep a process alive.

  python -m codegraph.watch <project_root> [poll_seconds]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

POLL_SECONDS = 2.0
IDLE_EXIT_SECONDS = 1800  # retire after 30 min with no detected changes


def _is_watcher(pid: int) -> bool:
    """Alive AND actually a codegraph watcher - a recycled PID from a stale pidfile
    (reboot, SIGKILL) must not stand the new watcher down forever."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return "codegraph.watch" in out
    except Exception:
        return True  # can't inspect -> assume it's a live watcher (standing down is safe)


def main() -> None:
    if len(sys.argv) < 2:
        return
    root = os.path.realpath(sys.argv[1])
    poll = float(sys.argv[2]) if len(sys.argv) > 2 else POLL_SECONDS
    if root == os.path.realpath(os.path.expanduser("~")):
        return  # never watch $HOME - a stray ~/.codegraph must not trigger a home-tree index
    db = os.path.join(root, ".codegraph", "graph.db")
    if not os.path.exists(db):
        return  # only watch indexed projects
    pidfile = os.path.join(root, ".codegraph", "watch.pid")

    # dedup: if a live watcher already owns this project, stand down. Stale claims
    # (dead or recycled PID) are removed, then the pidfile is claimed with O_EXCL so
    # two racing SessionStarts can't both think they won - the loser exits.
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            if old != os.getpid() and _is_watcher(old):
                return
        except Exception:
            pass
        try:
            os.remove(pidfile)
        except OSError:
            pass
    try:
        fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return  # lost the claim race to a sibling watcher

    from . import indexer as I
    last_change = time.time()
    try:
        while True:
            try:
                res = I.index(root, db)
                if res.get("reindexed") or res.get("removed"):
                    last_change = time.time()
            except Exception:
                pass  # transient (lock, mid-write) -> try again next poll
            if time.time() - last_change > IDLE_EXIT_SECONDS:
                break
            time.sleep(poll)
    finally:
        try:
            if open(pidfile).read().strip() == str(os.getpid()):
                os.remove(pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()
