"""`chimera install-hooks` — wire chimera's session hooks into Claude Code.

Idempotent merge into ~/.claude/settings.json:
  - PostToolUse hook on Edit|Write|MultiEdit|NotebookEdit → auto-log file touches
  - SessionStart hook → auto-read inbox notes from other sessions
  - UserPromptSubmit hook → periodic reminder to log decisions/questions

Doesn't clobber existing hooks. Adds chimera entries alongside whatever's
already configured. Re-running is safe (replaces by command match).

Removal: `chimera install-hooks --uninstall` strips chimera entries cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from chimera.log import get_logger

log = get_logger("cli.install_hooks")


SETTINGS_PATH = Path(os.environ.get("CLAUDE_SETTINGS_PATH", str(Path.home() / ".claude" / "settings.json")))
SCRIPTS_DIR = Path(__file__).resolve().parents[5] / "scripts" / "hooks"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "install-hooks",
        help="Wire chimera session hooks into Claude Code's settings.json.",
        description=(
            "Adds three hooks to ~/.claude/settings.json: PostToolUse "
            "(auto-log file touches), SessionStart (auto-read inbox), "
            "UserPromptSubmit (periodic decision/question reminder). "
            "Idempotent — safe to re-run."
        ),
    )
    p.add_argument(
        "--uninstall", action="store_true",
        help="Remove chimera hooks from settings.json instead of adding them.",
    )
    p.add_argument(
        "--settings-path", default=str(SETTINGS_PATH),
        help=f"Path to settings.json (default: {SETTINGS_PATH}).",
    )
    p.add_argument(
        "--scripts-dir", default=str(SCRIPTS_DIR),
        help=f"Path to chimera hook scripts (default: {SCRIPTS_DIR}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the resulting settings.json without writing.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    settings_path = Path(args.settings_path)
    scripts_dir = Path(args.scripts_dir).resolve()

    if not scripts_dir.is_dir():
        print(f"[chimera install-hooks] scripts dir not found: {scripts_dir}", flush=True)
        return 2

    # Load existing settings (or start empty)
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(
                f"[chimera install-hooks] {settings_path} has invalid JSON: {e}\n"
                "Refusing to overwrite — fix the file manually first.",
                flush=True,
            )
            return 3
    else:
        settings = {}

    if not isinstance(settings, dict):
        print(
            f"[chimera install-hooks] {settings_path} top-level isn't an object — refusing to modify.",
            flush=True,
        )
        return 3

    if args.uninstall:
        new_settings = _strip_chimera_hooks(settings)
        action = "removed"
    else:
        new_settings = _add_chimera_hooks(settings, scripts_dir)
        action = "installed"

    if args.dry_run:
        print("[chimera install-hooks] dry-run — would write:", flush=True)
        print(json.dumps(new_settings, indent=2))
        return 0

    # Backup first
    if settings_path.is_file():
        backup = settings_path.with_suffix(f".json.bak.{int(_mtime(settings_path))}")
        shutil.copy2(settings_path, backup)
        print(f"[chimera install-hooks] backup: {backup}", flush=True)

    # Atomic write
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_settings, indent=2) + "\n", encoding="utf-8")
    tmp.replace(settings_path)

    print(f"[chimera install-hooks] {action} chimera hooks at {settings_path}", flush=True)
    if not args.uninstall:
        print(
            "\nWhat's now active for new Claude Code sessions:",
            flush=True,
        )
        print(
            f"  • PostToolUse on Edit|Write|MultiEdit|NotebookEdit → "
            f"{scripts_dir / 'post_tool_use.py'}",
            flush=True,
        )
        print(
            f"  • SessionStart → {scripts_dir / 'session_start.py'} "
            "(auto-reads inbox from other sessions)",
            flush=True,
        )
        print(
            f"  • UserPromptSubmit (every 8 turns) → "
            f"{scripts_dir / 'user_prompt_submit.py'} "
            "(reminder to log decisions/questions)",
            flush=True,
        )
        print(
            "\nRestart Claude Code so it picks up the new settings.json.",
            flush=True,
        )
    return 0


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


_CHIMERA_MARKER = "_chimera_hook"


def _add_chimera_hooks(settings: dict, scripts_dir: Path) -> dict:
    """Merge chimera hooks into existing settings, replacing prior chimera
    entries by their marker but preserving non-chimera hooks."""
    out = dict(settings)
    hooks = out.setdefault("hooks", {}) if isinstance(out.get("hooks"), dict) or "hooks" not in out else out["hooks"]
    if not isinstance(hooks, dict):
        # Won't merge cleanly; preserve original under a side key + start fresh
        log.warning("settings.hooks wasn't a dict — preserving as 'hooks_invalid' and starting fresh")
        out["hooks_invalid"] = hooks
        hooks = {}
        out["hooks"] = hooks

    pt = scripts_dir / "post_tool_use.py"
    ss = scripts_dir / "session_start.py"
    ups = scripts_dir / "user_prompt_submit.py"

    # Each hook event accepts a list of matchers. We append the chimera entry
    # if not already present (matched by marker).
    _upsert_hook(hooks, "PostToolUse", {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": str(pt), _CHIMERA_MARKER: True}],
    })
    _upsert_hook(hooks, "SessionStart", {
        "hooks": [{"type": "command", "command": str(ss), _CHIMERA_MARKER: True}],
    })
    _upsert_hook(hooks, "UserPromptSubmit", {
        "hooks": [{"type": "command", "command": str(ups), _CHIMERA_MARKER: True}],
    })

    return out


def _upsert_hook(hooks: dict, event: str, entry: dict) -> None:
    """Add `entry` to hooks[event] (creating the list if missing). If a
    chimera-marked entry already exists for this event, replace it in place."""
    matchers = hooks.setdefault(event, [])
    if not isinstance(matchers, list):
        return  # malformed; skip silently to avoid clobbering

    # Remove any prior chimera entries
    matchers[:] = [
        m for m in matchers
        if not (
            isinstance(m, dict)
            and any(
                isinstance(h, dict) and h.get(_CHIMERA_MARKER)
                for h in m.get("hooks", [])
            )
        )
    ]
    matchers.append(entry)


def _strip_chimera_hooks(settings: dict) -> dict:
    """Remove every chimera-marked hook from settings."""
    out = dict(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    new_hooks: dict = {}
    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            new_hooks[event] = matchers
            continue
        kept = [
            m for m in matchers
            if not (
                isinstance(m, dict)
                and any(
                    isinstance(h, dict) and h.get(_CHIMERA_MARKER)
                    for h in m.get("hooks", [])
                )
            )
        ]
        if kept:
            new_hooks[event] = kept
    if new_hooks:
        out["hooks"] = new_hooks
    elif "hooks" in out:
        del out["hooks"]
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
