#!/usr/bin/env python3
"""Idempotently register codegraph's MCP server + live-policing hooks into a user's
Claude Code config, WITHOUT clobbering anything already there. Backs up every file it
touches. Re-runnable - running twice changes nothing the second time.

  ~/.claude.json            -> mcpServers.codegraph   (the MCP stdio server)
  ~/.claude/settings.json   -> hooks.{PreToolUse,PostToolUse,SessionStart}  (live policing)

Single-user install (no ~/.claude-work dual-account). Run via install.sh, or directly:
  python3 install/merge_config.py
"""
import json
import os
import shutil
import time

HOME = os.path.expanduser("~")
ENGINE = os.path.join(HOME, ".local", "lib", "codegraph")
# Honor CLAUDE_CONFIG_DIR (work/secondary accounts); default account is ~/.claude with the
# MCP registry at ~/.claude.json, a secondary account keeps both under its config dir.
_CFG = os.environ.get("CLAUDE_CONFIG_DIR")
CLAUDE = _CFG or os.path.join(HOME, ".claude")
CLAUDE_JSON = os.path.join(_CFG, ".claude.json") if _CFG else os.path.join(HOME, ".claude.json")


def _backup(path: str) -> None:
    if os.path.exists(path):
        b = f"{path}.bak-codegraph"
        if not os.path.exists(b):  # keep the first (pre-install) backup, don't churn it
            shutil.copy2(path, b)
            print(f"  backed up {path} -> {b}")


def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def register_mcp() -> None:
    path = CLAUDE_JSON
    data = _load(path)
    servers = data.setdefault("mcpServers", {})
    desired = {"type": "stdio",
               "command": os.path.join(ENGINE, ".venv", "bin", "codegraph"),
               "args": [], "env": {}}
    if servers.get("codegraph") == desired:
        print("  MCP server 'codegraph' already registered")
        return
    _backup(path)
    servers["codegraph"] = desired
    _save(path, data)
    print("  registered MCP server 'codegraph'")


def _has_command(arr: list, command: str) -> bool:
    return any(h.get("command") == command
              for group in arr for h in group.get("hooks", []))


def install_hooks() -> None:
    path = os.path.join(CLAUDE, "settings.json")
    data = _load(path)
    hooks = data.get("hooks", {})
    wanted = [
        ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit",
         "~/.claude/hooks/codegraph-police.sh pre", 8, "codegraph: auditing blast radius..."),
        ("PostToolUse", "Edit|Write|MultiEdit|NotebookEdit",
         "~/.claude/hooks/codegraph-police.sh post", 10, "codegraph: updating graph..."),
        ("SessionStart", None,
         "~/.claude/hooks/codegraph-watch.sh", 15, "codegraph: refreshing graph + watcher..."),
    ]
    if all(_has_command(hooks.get(event, []), command) for event, _, command, _, _ in wanted):
        print("  codegraph hooks already present in settings.json")
        return
    _backup(path)
    hooks = data.setdefault("hooks", {})
    for event, matcher, command, timeout, msg in wanted:
        arr = hooks.setdefault(event, [])
        if _has_command(arr, command):
            continue
        entry = {"type": "command", "command": command, "timeout": timeout, "statusMessage": msg}
        if matcher is not None:
            group = next((g for g in arr if g.get("matcher") == matcher), None)
            if group is None:
                arr.append({"matcher": matcher, "hooks": [entry]})
            else:
                group.setdefault("hooks", []).append(entry)
        else:  # SessionStart has no matcher; only join a group that's ALSO unrestricted -
               # appending into a user's matcher-restricted group would gate the watcher
               # on their matcher and it might never fire on a normal session start.
            group = next((g for g in arr if "matcher" not in g), None)
            if group is None:
                arr.append({"hooks": [entry]})
            else:
                group.setdefault("hooks", []).append(entry)
    _save(path, data)
    print("  installed codegraph hooks into settings.json")


if __name__ == "__main__":
    print("Registering codegraph with Claude Code...")
    register_mcp()
    install_hooks()
    print("Done.")
