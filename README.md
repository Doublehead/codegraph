# codegraph

[![tests](https://github.com/Doublehead/codegraph/actions/workflows/tests.yml/badge.svg)](https://github.com/Doublehead/codegraph/actions/workflows/tests.yml)

Blast-radius enforcement for coding agents. codegraph builds an AST call/dependency
graph of your repo (tree-sitter, 7 languages) and injects a symbol's callers into the
agent's context at the moment it edits the file:

```
codegraph BLAST RADIUS - includes/checkout.php has 47 caller(s) across 12 file(s).
Audit these before changing signatures/behavior:
  - process_order (includes/orders.php:81)
  - Cart.finalize (includes/cart.php:210)
  ...
```

Code graphs already exist. They answer questions, which assumes someone asks. An LLM
mid-task has usually decided the edit is small and doesn't ask - then breaks callers
it never opened. codegraph doesn't wait to be queried; a hook fires on every file
edit and puts the dependency list in front of the model, unprompted. Works on any
model, since reading a caller list requires no particular capability.

The hooks are warn-only and fail open. A warning never blocks an edit, and any
failure in the hook path exits silently. The graph is regenerated from source
incrementally, so it can't drift from the code.

## Install

Requirements: macOS or Linux, Python 3.10+. Claude Code for the hook integration;
the MCP server itself works with any MCP client.

```bash
git clone https://github.com/Doublehead/codegraph
cd codegraph
./install.sh
```

Then restart Claude Code and run `/codegraph-reindex` inside a project to build its
graph. Verify with:

```bash
claude mcp get codegraph      # should say: Connected
```

The installer copies the engine to `~/.local/lib/codegraph`, creates a venv with
pinned dependencies, runs the full test suite to verify the install, copies the
hooks, and registers the MCP server and hooks in Claude Code's config. It's
idempotent, backs up every config file it touches, and never clobbers existing
servers or hooks. Details in [INSTALL.md](INSTALL.md).

Per-project graph data lives in `<project>/.codegraph/` - add that to the project's
`.gitignore`.

Using another MCP client: skip the hook parts and register the stdio server binary
directly - `~/.local/lib/codegraph/.venv/bin/codegraph`.

Uninstall: `install/uninstall.sh`. Removes the server, hooks, and command
registration; leaves per-project `.codegraph/` dirs alone.

## Tools

| Tool | Answers |
|---|---|
| `index(root, force)` | Build/refresh the graph. Incremental by content hash; honours .gitignore. |
| `stats(root)` | Counts and resolution quality, by language and symbol kind. |
| `find(root, name)` | Where is this symbol defined? |
| `callers(root, symbol, depth)` | Reverse reachability - who breaks if this changes. |
| `callees(root, symbol, depth)` | Forward reachability - what it depends on. |
| `neighbors(root, symbol)` | Direct callers and callees with call-site lines. |
| `blast_radius(root, target)` | Impact set of a symbol or a whole file, grouped by file. |
| `path(root, src, dst)` | Shortest call path between two symbols. |
| `cycles(root)` | Circular dependencies. Exact edges only, so a report is trustworthy. |
| `hotspots(root, top)` | Load-bearing symbols by call-site fan-in, distinct dependents, betweenness. |
| `hooks(root, ...)` | String-dispatch coupling: listeners, fire sites, entry points, unauthenticated surface. |
| `pending(root)` | Edge drift from uncommitted edits, including cross-file resolution flips. |
| `scope(root, include, exclude)` | Scope a vendor monorepo down to your code. No args = dry-run suggestion. |

`symbol` accepts `Class.method` to disambiguate. On a large unscoped repo, `index`
returns a scope suggestion and the server instructions direct the agent to apply it;
nobody hand-writes config.

## Resolution model

A call resolves by its shape, derived from the receiver - never by name alone:

| Call shape | Targets | Example |
|---|---|---|
| bare `f()` | a free function; locals/params mask same-named globals | `save()` |
| `this/self/$this.m()` | a method of the same class | `self.save()` |
| `super`/`parent::m()` | a method of a parent class | `super.greet()` |
| `Class::m()` | that class's method | `Repo::flush()` |
| `x.m()` where `x = Foo()` | `Foo.m` via local type inference | `r = Repo(); r.save()` |
| `x.m()` where `x: Foo` declared | `Foo.m`, tiered `inferred` | `def f(r: Repo)` |
| `mod.f()` with `import mod` | a free function in that module's file | `util.helper()` |
| `x.m()`, `x` untyped | same-named methods, low confidence | `conn.send()` |

Every edge carries a confidence tier:

- `exact` - the call shape proves a single target. Trustworthy.
- `inferred` - a single plausible target, receiver type unverified. Disclosed, never
  silently exact.
- `ambiguous` - multiple candidates, edges to all. A real caller is never dropped.
- Calls with no in-repo target are recorded `unresolved`, not faked.

The bias is recall over precision: an uncertain edge is kept and labelled, never
dropped, because a missed caller is the failure this tool exists to prevent. Two
invariants hold throughout - no wrong `exact` edge, no dropped caller - and the
regression suite enforces both. Ruby mixins (`include`/`extend`/`prepend`) are part
of bare-call lookup; the js/ts/tsx dialects share one resolution namespace.

## Framework coupling

Most framework wiring never appears as a call. codegraph extracts it as a separate
evidence-gated `hook` edge tier:

- WordPress: `add_action`/`add_filter`/`do_action`/`apply_filters`/`register_rest_route`.
  `hooks(entry_points=true)` maps the ajax/REST attack surface with unauthenticated
  endpoints flagged. All callback forms resolve; a collision resolves to nothing
  rather than the wrong symbol.
- Django signals: `@receiver`/`.connect` paired with `.send`/`.send_robust`. Evidence-
  gated, so a socket's `.connect`/`.send` can't fabricate an edge.
- Celery: `@task`/`@shared_task` paired with `.delay`/`.apply_async`.
- Web routes: Flask/FastAPI decorators, Django `path()`/`re_path()`, as entry points.
- React: JSX `<Component/>` usage becomes a caller edge; HOC-wrapped components
  register as definitions.

Distinct mechanisms never cross-link. Blind spots that static analysis can't see
(interpolated hook names, variable callbacks, closures) resolve to nothing and are
disclosed, never faked.

## Languages

| Language | Extensions |
|---|---|
| Python | `.py` |
| JavaScript | `.js` `.jsx` `.mjs` `.cjs` |
| TypeScript | `.ts` `.mts` `.cts` |
| TSX | `.tsx` (separate grammar; the plain `.ts` dialect has no JSX nodes) |
| PHP | `.php` |
| Ruby | `.rb` `.rake` |
| Go | `.go` |

A grammar that fails to load degrades that one language, not the server. Want another?
Open an issue.

## Branches and worktrees

The graph always reflects the current working tree. File discovery is `git ls-files
-co` (tracked plus untracked, minus ignored) and every file is read and hashed off
disk, not from git objects. So the graph sees whatever branch is checked out plus any
uncommitted or unmerged edits sitting on top of it.

Branch switches self-heal. `git checkout` rewrites the mtime on every file that
differs between branches, so the incremental pass re-parses exactly those and skips
the rest; the background watcher, SessionStart, and the post-edit hook all trigger it,
so the graph is current within a couple seconds of a checkout.

One caveat: there is a single `.codegraph/graph.db` per project directory, shared
across branches. It mirrors what is checked out right now, not all branches at once.
In the brief window between a `git checkout` and the next reindex tick, a query can
still reflect the branch you just left; run `/codegraph-reindex` (or `index(force=True)`)
to make it current immediately.

For true parallel branches, use git worktrees. Each worktree is its own directory and
the graph resolution walks up to the nearest `.codegraph/`, so two worktrees get two
independent graphs that never touch each other.

## Threat model

codegraph is a local stdio MCP server running single-user at the same OS privilege
as the calling agent, which already has file and shell access. There is no privilege
boundary here. Root guards, parameter clamps, and size caps exist as footgun
protection, not as a security boundary. The bug classes that matter are crashes,
hangs, data corruption, and wrong `exact` edges.

## Failure handling

- Every hook failure path exits 0 silently. A malformed scope config indexes
  everything rather than crashing.
- A corrupt graph DB is quarantined and rebuilt from source, disclosed in the result.
- A crash between file commits and edge resolution leaves a persisted flag that
  forces re-resolution on the next run, so a half-written graph is never trusted.
- Schema migrations check every column any INSERT uses against old DBs on open.

## Tests

```bash
python tests/test_codegraph.py     # plain python; also pytest-compatible
```

62 tests: a 6-language ground truth plus regressions from four adversarial audit
rounds. Each confirmed defect became a permanent test with decoys. Run it after any
change to the parser, resolver, or hooks.

## Known limitations

- Shape-based resolution has a precision ceiling versus full type analysis. The long
  tail lands in `inferred`/`ambiguous`, not in a wrong `exact`.
- Ruby paren-less calls in expression position (`x = helper + 1`) are not extracted.
- Member-expression JSX (`<Animated.View/>`) is skipped.
- JS namespace-import resolution requires the alias to match the filename stem.
- Python re-exports (`util.helper` defined in `util/_impl.py`) land `unresolved`.
- Windows is untested; the hook glue is bash.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free for noncommercial use -
individuals, personal projects, education, research, charities, government.

Commercial use - including inside a for-profit company's development workflow -
requires a commercial license: **ccgraphtheory@shaunoster.com**.

Required Notice: Copyright (c) 2026 Shaun Oster (https://github.com/Doublehead)
