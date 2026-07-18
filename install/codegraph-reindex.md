---
description: Force-reindex the codegraph graph for this project (full reparse, rebuilds every edge)
---

The user wants to FORCE-reindex the codegraph graph for this project — a full reparse
that ignores the content-hash cache and rebuilds every edge from scratch (use this
after a codegraph resolver change, or to repair a graph you suspect is stale/corrupt).

Do this now:
1. Call `mcp__codegraph__index` with `force: true` and `root` set to `$ARGUMENTS` if the
   user provided a path, otherwise `"."` (the current project).
2. Report the result concisely: files indexed, and the edge breakdown
   (exact / inferred / ambiguous) plus unresolved count, from the returned stats.
3. If 0 files were discovered, tell the user this project has no codegraph-supported
   source at that root (so nothing to police here).

Do not edit any code. This is a read-then-index operation only.
