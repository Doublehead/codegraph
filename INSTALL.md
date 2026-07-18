# Installing codegraph on a Mac

One command sets up the engine, the MCP server, and the live-policing hooks. Idempotent
and re-runnable; it backs up any config it edits and never clobbers existing hooks/servers.

## Prerequisites

- **Python 3.10+** (`python3 --version`)
- **Claude Code** installed (the `claude` CLI / app)
- Network access (the install pulls pinned `mcp` + `tree-sitter` wheels from PyPI)

## Install

1. Get the source onto the machine - copy this whole folder (AirDrop / `scp` / `rsync` /
   a git clone) to anywhere, e.g. `~/Downloads/codegraph`.
2. From that folder:

   ```bash
   ./install.sh
   ```

3. **Restart Claude Code.**
4. In any project, build its graph once:

   ```
   /codegraph-reindex
   ```

That's it. Verify the server is live:

```bash
claude mcp get codegraph     # should say: Connected
```

## What it does

| Step | Where |
|------|-------|
| Copies the engine package | `~/.local/lib/codegraph/` |
| Creates an isolated venv + installs pinned deps | `~/.local/lib/codegraph/.venv/` |
| Runs the regression suite to verify the install | (must print `NN/NN passed`) |
| Installs the policing hooks + reindex command | `~/.claude/hooks/`, `~/.claude/commands/` |
| Registers the MCP server | `~/.claude.json` → `mcpServers.codegraph` |
| Wires the Pre/PostToolUse + SessionStart hooks | `~/.claude/settings.json` → `hooks` |

Every edited config file gets a one-time `*.bak-codegraph` backup. Re-running the
installer is safe - it detects what's already in place and changes nothing.

## How it works once installed

- **Before any edit**, a PreToolUse hook injects the symbol's blast radius (its callers)
  into context - so the agent audits dependents before changing a signature/behavior.
- **After an edit**, a PostToolUse hook incrementally refreshes the graph.
- **At session start**, the graph is brought current and a lightweight watcher is launched
  for changes made outside the agent (IDE saves, `git pull`).
- The graph for each project lives in that project's `.codegraph/graph.db` (git-ignored).

It maps not just the call graph but framework string-dispatch coupling the AST can't see:
WordPress hooks, Django signals, Celery tasks, and web routes (entry points).

## Scoping a large / vendor monorepo (the agent does this for you)

On a repo that's mostly third-party code (WordPress + WooCommerce/Dokan, large vendor trees),
the graph should index only the code you actually edit - otherwise hotspots and ambiguity are
dominated by vendor symbols. **You don't configure this by hand - the agent does it.** When
`index`/`/codegraph-reindex` runs on a big unscoped repo, the result carries a `scope_suggestion`
and the server instructions tell the agent to apply it: it calls the `scope` tool, which writes
`<project>/.codegraph/config.json` (excluding confirmed vendor - WP core, WooCommerce, Dokan,
parent themes…) and reindexes. Your own code is never excluded, and anything you add later stays
in automatically.

If you ever want to set it manually, the config is just:

```json
{
  "exclude": ["wp-content/plugins/woocommerce/*", "wp-content/plugins/dokan/*"],
  "include": ["mu-plugins/*", "wp-content/themes/your-child-theme/*"]
}
```

- **`exclude`** (recommended) - dropped on top of the built-in backup/editor defaults; safe
  because it only removes named vendor trees, never new code you write.
- **`include`** - if present, ONLY matching paths index (tightest scope; everything else is
  treated as external/unresolved).
- Globs are fnmatch-style relative to the project root; `*` spans directories. A malformed
  config is ignored (indexes everything) rather than breaking.

Apply changes with `/codegraph-reindex` (or the agent's `scope` tool, which reindexes for you).

## Reading the metrics

`stats` / `index` report resolution by **call site** (`edges_exact` / `edges_inferred` /
`edges_ambiguous` / `unresolved`) - an ambiguous call to N candidates is one ambiguous
decision, not N. `edge_rows` is the separate fanned-out candidate-edge total (graph size).

## Troubleshooting: "I'm not seeing blast-radius injections"

Field experience says the report is usually wrong - the agent's own context is the
least reliable witness (long sessions lose retrieval over their own history). Check
in this order:

1. Count actual injections in the session transcript - this is ground truth:

   ```bash
   grep -c hook_additional_context ~/.claude/projects/<project-slug>/<session>.jsonl
   ```

2. Remember the silent-by-design cases: a file with no symbols in the graph, a
   brand-new file, a project with no governing `.codegraph/` graph up-tree, and any
   mutation made through Bash (heredocs, `sed`) - the hook matches Edit/Write tool
   calls only.

3. Live probe: pick a file with known callers (`callers <symbol>` first), make a
   trivial comment edit with the Edit tool, and check the count incremented. The
   warning fires on the edit action itself, so no destructive change is needed.

If the count increments but nothing ever reaches the model, then it's real - check
that the hook is registered under `PreToolUse` in `~/.claude/settings.json` and that
the script runs manually:

```bash
echo '{"tool_input":{"file_path":"/abs/path/to/file.php"}}' | ~/.claude/hooks/codegraph-police.sh pre
```

## Uninstall

```bash
install/uninstall.sh
```

Unregisters the MCP server + hooks, removes the hook/command files, and offers to delete
the engine. Per-project `.codegraph/` graph dirs are left untouched (delete them yourself
if you want).

## Notes

- **Single-user install.** If you run Claude Code under a separate config dir
  (`CLAUDE_CONFIG_DIR`, e.g. a work account), run the installer once per config dir with
  that variable exported.
- No internet at install time → the venv/dep step fails; everything else is local.
