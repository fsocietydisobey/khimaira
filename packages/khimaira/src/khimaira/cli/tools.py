"""`khimaira tools` — discoverability surface for everything khimaira exposes.

Lists CLI subcommands, MCP tools, slash commands, and web routes in one
catalog. Filterable by category or substring.

Usage:
    khimaira tools                       # full catalog
    khimaira tools session               # only items matching "session"
    khimaira tools --category mcp        # only MCP tools
    khimaira tools --json                # machine-readable
    khimaira tools cost                  # everything related to cost

Designed to be cheap to scan visually — each tool gets one line with a
description short enough to fit on a terminal width of 100. Long bodies
are linked-out (file path or URL), not inlined.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PACKAGE_ROOT = _HERE.parent  # khimaira/
_REPO_ROOT = _PACKAGE_ROOT.parent.parent.parent.parent  # khimaira/ (workspace)


def _collect_cli_subcommands() -> list[dict]:
    """Walk argparse subparsers from the main entry point.

    Returns one record per top-level subcommand: name, help, module path.
    """
    from khimaira.cli import main as _main_fn  # noqa: F401  (import side-effects)
    from khimaira.cli import (
        attach as attach_cmd,
        dev,
        doctor,
        install_hooks,
        mcp_serve,
        monitor,
        observer as observer_cli,
        route,
        task,
        tools as tools_cmd,
    )

    out: list[dict] = []
    modules = {
        "task": task,
        "route": route,
        "dev": dev,
        "doctor": doctor,
        "monitor": monitor,
        "mcp": mcp_serve,
        "install-hooks": install_hooks,
        "attach": attach_cmd,
        "observer": observer_cli,
        "tools": tools_cmd,
    }
    # Build a throwaway parser to extract help text per subcommand
    parser = argparse.ArgumentParser(prog="khimaira")
    sub = parser.add_subparsers(dest="cmd")
    for name, mod in modules.items():
        if not hasattr(mod, "add_subparser"):
            continue
        try:
            mod.add_subparser(sub)
        except Exception:
            pass

    # Pull parser help via the registered subparsers action
    for action in parser._actions:  # type: ignore[attr-defined]
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            help_text = subparser.description or ""
            # Strip leading whitespace from multi-line descriptions
            help_text = re.split(r"\n\n", help_text.strip(), maxsplit=1)[0]
            help_text = " ".join(help_text.split())
            out.append(
                {
                    "category": "cli",
                    "name": f"khimaira {name}",
                    "description": help_text[:140] if help_text else "(no help)",
                    "source": f"khimaira.cli.{name.replace('-', '_')}",
                }
            )
    out.sort(key=lambda r: r["name"])
    return out


def _mcp_call_counts(window_minutes: int = 60 * 24 * 7) -> dict[str, int]:
    """Per-tool call count over the window (default: 7 days), from the
    local mcp-calls.jsonl. Returns {} if telemetry log missing/unreadable.

    Local file read — no daemon dependency, safe to call even when
    khimaira-monitor is down.
    """
    try:
        from khimaira.monitor import mcp_calls
    except ImportError:
        return {}
    try:
        digest = mcp_calls.summarize(window_minutes=window_minutes)
    except Exception:
        return {}
    return {entry["tool"]: entry.get("calls", 0) for entry in digest.get("by_tool", [])}


def _collect_mcp_tools() -> list[dict]:
    """Introspect khimaira.server.mcp module for @mcp.tool() decorated functions.

    Pulls each tool's docstring (first line) as the description, plus
    the function signature so users can see the args inline. Attaches a
    `call_count` field from the local 7-day call telemetry so the rendered
    listing can sort by usage — the most-used tools surface first, which
    is what an agent scanning the catalog actually wants.
    """
    from khimaira.server import mcp as mcp_mod

    call_counts = _mcp_call_counts()

    out: list[dict] = []
    for name in dir(mcp_mod):
        obj = getattr(mcp_mod, name)
        if not callable(obj):
            continue
        # MCP tool functions are async + have __wrapped__ from logged_tool
        if not (inspect.iscoroutinefunction(obj) or hasattr(obj, "__wrapped__")):
            continue
        # Heuristic: skip helpers / private / non-tool funcs.
        if name.startswith("_"):
            continue
        # Skip things that don't look like our naming convention (chain/
        # session/monitor/observer/follow/spawn/list/kill/wait/usage/
        # health/status/approve/research/architect/brainstorm/classify/
        # history/rewind/swarm).
        prefixes = (
            "session_",
            "monitor_",
            "chain",
            "follow_",
            "spawn_",
            "list_",
            "kill_",
            "wait_",
            "usage_",
            "research",
            "architect",
            "brainstorm",
            "classify",
            "approve",
            "history",
            "rewind",
            "swarm",
            "health",
            "status",
        )
        if not any(name.startswith(p) for p in prefixes):
            continue
        underlying = getattr(obj, "__wrapped__", obj)
        sig = ""
        try:
            sig = str(inspect.signature(underlying))
        except (TypeError, ValueError):
            sig = "(...)"
        doc = inspect.getdoc(underlying) or ""
        # First non-empty line of the docstring as description
        desc = ""
        for line in doc.split("\n"):
            line = line.strip().lstrip("*").strip()
            if line:
                desc = line
                break
        out.append(
            {
                "category": "mcp",
                "name": f"mcp__khimaira__{name}",
                "description": desc[:200] if desc else "(no docstring)",
                "signature": sig,
                "source": "khimaira.server.mcp",
                "call_count": call_counts.get(name, 0),
            }
        )
    # Rank by 7-day call count desc, alphabetic tiebreaker. The most-
    # used tools surface first — which is what an agent scanning the
    # catalog actually wants. When no telemetry exists (fresh install),
    # all counts are 0 and the sort degenerates cleanly to alphabetic.
    out.sort(key=lambda r: (-r["call_count"], r["name"]))
    return out


def _collect_slash_commands() -> list[dict]:
    """Walk ~/.claude/commands/*.md for available slash commands."""
    cmds_dir = Path(os.path.expanduser("~/.claude/commands"))
    if not cmds_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(cmds_dir.glob("*.md")):
        name = path.stem
        # First H1 line OR first paragraph as description
        desc = ""
        try:
            text = path.read_text(encoding="utf-8")
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("# /"):
                    # Header like "# /inbox — read pending answers..."
                    desc = (
                        line.lstrip("#").strip().split(" — ", 1)[-1]
                        if " — " in line
                        else line.lstrip("#").strip()
                    )
                    break
                if line.startswith("# ") or line.startswith("## "):
                    continue
                desc = line[:200]
                break
        except OSError:
            pass
        out.append(
            {
                "category": "slash",
                "name": f"/{name}",
                "description": desc[:200] if desc else "(no description)",
                "source": str(path),
            }
        )
    return out


def _collect_web_routes() -> list[dict]:
    """Hardcoded list of khimaira-monitor SPA routes worth surfacing.

    Could parse App.tsx but the route list is tiny + stable; hardcode.
    """
    return [
        {
            "category": "web",
            "name": "/",
            "description": "Project index — all attached / scanned projects",
            "source": "http://127.0.0.1:8740/",
        },
        {
            "category": "web",
            "name": "/{project}",
            "description": "Live LangGraph topology + run replay for a project",
            "source": "http://127.0.0.1:8740/{project}",
        },
        {
            "category": "web",
            "name": "/{project}/cost",
            "description": "Estimated USD spend by model + telemetry overhead callout",
            "source": "http://127.0.0.1:8740/{project}/cost",
        },
        {
            "category": "web",
            "name": "/{project}/trace/{cid}",
            "description": "Trace waterfall for one app run (chain/llm/tool/external bars on time axis)",
            "source": "http://127.0.0.1:8740/{project}/trace/{correlation_id}",
        },
    ]


def _collect_observer_endpoints() -> list[dict]:
    """REST API endpoints worth knowing about (for scripting / debugging)."""
    return [
        {
            "category": "api",
            "name": "POST /api/heartbeat",
            "description": "Receive one event from khimaira_observer (called by app, not user)",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/heartbeats/{project}",
            "description": "List active runs in project",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/heartbeats/{project}/cost",
            "description": "Per-model token usage + estimated USD",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/heartbeats/{project}/slow",
            "description": "Recent slow chain/llm/tool/external calls",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/heartbeats/{project}/by-correlation/{cid}",
            "description": "All events for one app-level run (auto-correlation)",
            "source": "",
        },
        {
            "category": "api",
            "name": "POST /api/handoffs",
            "description": "Drop a handoff for any future session in matching cwd",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/sessions/{sid}/transcript/summary",
            "description": "Heuristic summary of a session's transcript (no LLM)",
            "source": "",
        },
        {
            "category": "api",
            "name": "GET /api/sessions/{sid}/transcript/query?q=",
            "description": "Grep a session's transcript for a substring",
            "source": "",
        },
    ]


def _collect_all() -> list[dict]:
    """Collect everything; failure in one category doesn't kill the rest."""
    out: list[dict] = []
    for fn in (
        _collect_cli_subcommands,
        _collect_mcp_tools,
        _collect_slash_commands,
        _collect_web_routes,
        _collect_observer_endpoints,
    ):
        try:
            out.extend(fn())
        except Exception as e:
            out.append(
                {
                    "category": "_error",
                    "name": fn.__name__,
                    "description": f"failed to collect: {e}",
                    "source": "",
                }
            )
    return out


_CATEGORY_LABELS = {
    "cli": "📟 CLI subcommands",
    "mcp": "🔌 MCP tools",
    "slash": "⚡ Claude Code slash commands",
    "web": "🌐 khimaira-monitor web routes",
    "api": "🔧 REST API endpoints",
    "_error": "⚠️  errors",
}


def _print_text(items: list[dict]) -> None:
    by_cat: dict[str, list[dict]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    for cat in ("cli", "slash", "web", "mcp", "api", "_error"):
        rows = by_cat.get(cat) or []
        if not rows:
            continue
        suffix = ""
        if cat == "mcp":
            # Flag the ordering so users don't think the list is alphabetic.
            total_calls = sum(r.get("call_count", 0) for r in rows)
            if total_calls > 0:
                suffix = "  — ranked by 7-day call count"
        print(f"\n{_CATEGORY_LABELS.get(cat, cat)}  ({len(rows)}){suffix}")
        print("─" * 80)
        for it in rows:
            name = it["name"]
            desc = it["description"]
            if cat == "mcp" and it.get("signature"):
                count = it.get("call_count", 0)
                count_tag = f"  [{count}×/7d]" if count else ""
                print(f"  {name}{it['signature']}{count_tag}")
                print(f"      {desc}")
            else:
                # Pad name to a column then description
                print(f"  {name:<40}  {desc}")


def _filter(items: list[dict], q: str | None, category: str | None) -> list[dict]:
    out = items
    if category:
        out = [i for i in out if i["category"] == category]
    if q:
        ql = q.lower()
        out = [
            i for i in out if ql in i["name"].lower() or ql in i["description"].lower()
        ]
    return out


def _cmd_tools(args: argparse.Namespace) -> int:
    items = _collect_all()
    items = _filter(items, args.query, args.category)
    if args.json:
        json.dump(items, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    if not items:
        print(f"no tools matching query={args.query!r} category={args.category!r}")
        return 1
    _print_text(items)
    print(
        f"\n{len(items)} item(s). Use --json for machine-readable output, "
        f"or `khimaira tools <substring>` to filter."
    )
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "tools",
        help="discoverability surface — list CLI subcommands, MCP tools, slash commands, web routes",
        description=(
            "List everything khimaira exposes: CLI subcommands, MCP tools, "
            "Claude Code slash commands, khimaira-monitor web routes, and "
            "REST API endpoints. Filter by substring or category."
        ),
    )
    p.add_argument(
        "query",
        nargs="?",
        default=None,
        help="optional substring filter (matched against name + description)",
    )
    p.add_argument(
        "--category",
        choices=["cli", "mcp", "slash", "web", "api"],
        help="restrict to one category",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_cmd_tools)
