"""`chimera install-hooks` — wire chimera's session hooks into Claude Code.

Idempotent merge into ~/.claude/settings.json:
  - PostToolUse hook on Edit|Write|MultiEdit|NotebookEdit → auto-log file touches
  - SessionStart hook → auto-read inbox notes from other sessions
  - UserPromptSubmit hook → periodic reminder to log decisions/questions

Doesn't clobber existing hooks. Adds chimera entries alongside whatever's
already configured. Re-running is safe (replaces by command match).

Hook commands take the form `<python> -m chimera.hooks.<name>` rather
than embedding a filesystem path to a hook script. This works
identically for source-checkout installs and wheel installs — the
hook modules live in the chimera package itself (chimera/hooks/*.py),
so importlib resolves them either way. Previous design embedded a path
to `scripts/hooks/<name>.py` at workspace root, which crashed under
`pip install` because wheels strip workspace-level files.

Removal: `chimera install-hooks --uninstall` strips chimera entries cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from importlib import resources
from pathlib import Path

from chimera.log import get_logger

log = get_logger("cli.install_hooks")


SETTINGS_PATH = Path(
    os.environ.get(
        "CLAUDE_SETTINGS_PATH", str(Path.home() / ".claude" / "settings.json")
    )
)

# Legacy: the workspace-root scripts dir we used pre-chimera.hooks-package.
# Retained for the --scripts-dir CLI flag's default so users on old
# settings.json files can still uninstall by matching the legacy command.
# New writes always use the python -m form (see _build_hook_command).
LEGACY_SCRIPTS_DIR = Path(__file__).resolve().parents[5] / "scripts" / "hooks"


def _build_hook_command(module_basename: str) -> str:
    """Construct the shell command Claude Code will execute for a hook.

    Form: `<python interpreter> -m chimera.hooks.<module>`. The interpreter
    is captured from sys.executable at install time — that's the
    interpreter the user invoked chimera with, so it's the one that has
    chimera importable. Stays stable until they reinstall chimera elsewhere.

    Each piece is shell-quoted so spaces in paths (e.g. macOS
    "/Users/Joe Smith/...") survive. shlex.quote on a single token is
    a no-op when not needed, so this is safe to apply unconditionally.
    """
    return f"{shlex.quote(sys.executable)} -m chimera.hooks.{module_basename}"


def _hooks_package_dir() -> Path | None:
    """Path to chimera/hooks/ on the local filesystem, or None if the
    package isn't on importable file paths (rare — e.g. a zipped wheel).

    Used by the `--scripts-dir` legacy flag for compatibility with
    older settings.json files that referenced filesystem paths
    directly. New installs don't depend on this.
    """
    try:
        from chimera import hooks as hooks_pkg

        traversable = resources.files(hooks_pkg)
        # importlib.resources.files() returns MultiplexedPath/Path-like.
        # str() gives us the on-disk location for normal installs.
        return Path(str(traversable))
    except (ImportError, ModuleNotFoundError):
        return None


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
        "--uninstall",
        action="store_true",
        help="Remove chimera hooks from settings.json instead of adding them.",
    )
    p.add_argument(
        "--settings-path",
        default=str(SETTINGS_PATH),
        help=f"Path to settings.json (default: {SETTINGS_PATH}).",
    )
    p.add_argument(
        "--scripts-dir",
        default=None,
        help=(
            "(Deprecated) Path to a directory of hook .py scripts. "
            "New installs use `python -m chimera.hooks.<name>` and don't "
            "need this. Kept for back-compat / non-standard chimera "
            "layouts."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resulting settings.json without writing.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    settings_path = Path(args.settings_path)

    # scripts_dir is deprecated — only matters for explicit overrides or
    # uninstall-on-legacy-settings. New writes go through the package
    # form via _add_chimera_hooks, which doesn't touch scripts_dir.
    legacy_scripts_dir: Path | None = None
    if args.scripts_dir:
        legacy_scripts_dir = Path(args.scripts_dir).resolve()
        if not legacy_scripts_dir.is_dir():
            print(
                f"[chimera install-hooks] --scripts-dir {legacy_scripts_dir} "
                f"not a directory (ignoring; using package hooks instead)",
                flush=True,
            )
            legacy_scripts_dir = None

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
        new_settings = _add_chimera_hooks(settings)
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

    print(
        f"[chimera install-hooks] {action} chimera hooks at {settings_path}", flush=True
    )
    if not args.uninstall:
        print(
            "\nWhat's now active for new Claude Code sessions:",
            flush=True,
        )
        print(
            f"  • PostToolUse on Edit|Write|MultiEdit|NotebookEdit → "
            f"{_build_hook_command('post_tool_use')}",
            flush=True,
        )
        print(
            f"  • SessionStart → {_build_hook_command('session_start')} "
            "(auto-reads inbox from other sessions)",
            flush=True,
        )
        print(
            f"  • UserPromptSubmit (every 8 turns) → "
            f"{_build_hook_command('user_prompt_submit')} "
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


def _add_chimera_hooks(settings: dict) -> dict:
    """Merge chimera hooks into existing settings, replacing prior chimera
    entries by their marker but preserving non-chimera hooks.

    Command form: `<python> -m chimera.hooks.<module>`. The python is
    whatever interpreter the user invoked chimera with at install time;
    it's the one that has chimera importable. See module-level
    _build_hook_command for details.
    """
    out = dict(settings)
    hooks = (
        out.setdefault("hooks", {})
        if isinstance(out.get("hooks"), dict) or "hooks" not in out
        else out["hooks"]
    )
    if not isinstance(hooks, dict):
        # Won't merge cleanly; preserve original under a side key + start fresh
        log.warning(
            "settings.hooks wasn't a dict — preserving as 'hooks_invalid' and starting fresh"
        )
        out["hooks_invalid"] = hooks
        hooks = {}
        out["hooks"] = hooks

    pt_cmd = _build_hook_command("post_tool_use")
    ss_cmd = _build_hook_command("session_start")
    ups_cmd = _build_hook_command("user_prompt_submit")

    # Each hook event accepts a list of matchers. We append the chimera entry
    # if not already present (matched by marker).
    _upsert_hook(
        hooks,
        "PostToolUse",
        {
            "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "command": pt_cmd, _CHIMERA_MARKER: True}],
        },
    )
    _upsert_hook(
        hooks,
        "SessionStart",
        {
            "hooks": [{"type": "command", "command": ss_cmd, _CHIMERA_MARKER: True}],
        },
    )
    _upsert_hook(
        hooks,
        "UserPromptSubmit",
        {
            "hooks": [{"type": "command", "command": ups_cmd, _CHIMERA_MARKER: True}],
        },
    )

    return out


def _upsert_hook(hooks: dict, event: str, entry: dict) -> None:
    """Add `entry` to hooks[event] (creating the list if missing). If a
    chimera-marked entry already exists for this event, replace it in place."""
    matchers = hooks.setdefault(event, [])
    if not isinstance(matchers, list):
        return  # malformed; skip silently to avoid clobbering

    # Remove any prior chimera entries
    matchers[:] = [
        m
        for m in matchers
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
            m
            for m in matchers
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
