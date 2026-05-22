"""JSON-merge primitive for surgical PreToolUse hook injection into settings.local.json.

This is the FIRST settings.local.json hook injector in khimaira — new design work,
not adoption of an existing pattern (per architect-1 Phase 2 must-fix #1).

Contract:
- inject_hook_entry: add or replace a single PreToolUse entry by marker. All other
  hook entries (other PreToolUse entries, PostToolUse, etc.) are preserved byte-exactly.
- remove_hook_entry: remove a single PreToolUse entry by marker. Same preservation.
- Atomic write: temp file + rename — never in-place mutate (settings.local.json is
  read by every Claude Code subprocess launch).
- Marker identifies the entry by checking the command field for a substring match
  (e.g., "themis_pretool.py"). First match wins.

settings.local.json Claude Code format:
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|...",
        "hooks": [{"type": "command", "command": "/abs/path/to/script.py"}]
      }
    ],
    "PostToolUse": [...]
  },
  ... other settings ...
}

Only the "hooks.PreToolUse" list is touched. Other keys at any level are preserved.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Marker string used to identify our PreToolUse entry in command fields
THEMIS_MARKER = "themis_pretool.py"

# Rules YAML directory relative to this file's package root
_RULES_DIR = Path(__file__).resolve().parents[1] / "monitor" / "api"


def _read_settings(settings_path: Path) -> tuple[dict, bool]:
    """Read settings.local.json. Returns (data, had_trailing_newline).

    Returns ({}, True) if absent or corrupt — new file will get trailing newline.
    """
    if not settings_path.exists():
        return {}, True
    try:
        raw = settings_path.read_text(encoding="utf-8")
        return json.loads(raw), raw.endswith("\n")
    except (OSError, json.JSONDecodeError):
        return {}, True


def _write_settings_atomic(
    settings_path: Path,
    data: dict,
    trailing_newline: bool = True,
) -> None:
    """Write settings atomically via temp file + rename.

    Creates parent .claude/ directory if needed. Uses same directory for
    temp file so rename is atomic on POSIX (same filesystem).

    trailing_newline: when True (default for new files), append a final \\n.
    Callers that are updating an existing file should pass the original file's
    trailing_newline state to preserve byte-identical round-trips.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    if trailing_newline:
        content += "\n"
    fd, tmp_path_str = tempfile.mkstemp(
        dir=settings_path.parent,
        suffix=".tmp",
        prefix="settings.local.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_path_str).replace(settings_path)
    except Exception:
        try:
            Path(tmp_path_str).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def inject_hook_entry(
    settings_path: Path,
    matcher: str,
    command: str,
    marker: str = THEMIS_MARKER,
) -> None:
    """Add or replace a PreToolUse hook entry in settings.local.json.

    Identified by `marker` (substring of the entry's `command` field).
    If an entry with the marker already exists, it is replaced in-place.
    If absent, a new entry is appended.

    All other hook entries (PreToolUse and other hook types) are preserved
    byte-exactly — no structural changes outside the matched/new entry.

    Atomic write: temp file + rename.
    """
    data, orig_trailing_nl = _read_settings(settings_path)
    data.setdefault("hooks", {})
    data["hooks"].setdefault("PreToolUse", [])

    new_entry = {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }

    entries: list[dict] = data["hooks"]["PreToolUse"]
    for i, entry in enumerate(entries):
        existing_cmd = _extract_command(entry)
        if existing_cmd and marker in existing_cmd:
            entries[i] = new_entry
            _write_settings_atomic(settings_path, data, trailing_newline=orig_trailing_nl)
            return

    # Not found — append
    entries.append(new_entry)
    _write_settings_atomic(settings_path, data, trailing_newline=orig_trailing_nl)


def remove_hook_entry(
    settings_path: Path,
    marker: str = THEMIS_MARKER,
) -> bool:
    """Remove a PreToolUse hook entry whose command contains marker.

    Returns True if found and removed, False if not found.
    All other hook entries survive. Atomic write: temp file + rename.
    Preserves the original file's trailing newline state for byte-identical round-trips.
    """
    data, orig_trailing_nl = _read_settings(settings_path)
    pre_tool_use: list[dict] = data.get("hooks", {}).get("PreToolUse", [])
    original_len = len(pre_tool_use)

    filtered = [
        entry
        for entry in pre_tool_use
        if not (marker in (_extract_command(entry) or ""))
    ]

    if len(filtered) == original_len:
        return False  # nothing removed

    data["hooks"]["PreToolUse"] = filtered
    # If PreToolUse list is now empty, remove the key to keep settings clean
    if not filtered:
        del data["hooks"]["PreToolUse"]
    if not data.get("hooks"):
        del data["hooks"]

    _write_settings_atomic(settings_path, data, trailing_newline=orig_trailing_nl)
    return True


def _extract_command(entry: dict) -> str | None:
    """Extract the first command string from a hook entry, or None."""
    try:
        hooks = entry.get("hooks") or []
        for h in hooks:
            cmd = h.get("command")
            if cmd:
                return cmd
    except (AttributeError, TypeError):
        pass
    return None


def derive_matcher_pattern() -> str:
    """Derive the PreToolUse matcher pattern from all themis rule YAMLs.

    Returns a pipe-separated string of all unique `tool:` values across every
    invariant in every role's rule file.

    Falls back to the hardcoded spec baseline if themis package is not installed
    or rule YAMLs can't be loaded.
    """
    _SPEC_BASELINE = (
        "Edit|Write|MultiEdit|NotebookEdit|Bash|Task"
        "|mcp__khimaira__auto|mcp__khimaira__delegate|mcp__khimaira__research"
        "|mcp__khimaira-chat__chat_send|mcp__khimaira-chat__chat_send_to"
        "|mcp__khimaira-chat__chat_history|mcp__khimaira-chat__chat_task_create"
    )
    try:
        from themis.data import load_all_rules

        rule_sets = load_all_rules()
        tools: set[str] = set()
        for rs in rule_sets:
            for inv in rs.invariants:
                for m in inv.matchers:
                    if m.tool:
                        tools.add(m.tool)
        if tools:
            return "|".join(sorted(tools))
    except Exception:
        pass
    return _SPEC_BASELINE


def resolve_hook_command(project_path: Path) -> str:
    """Build the absolute hook command string for a given project.

    Uses the project's own .venv Python interpreter and the khimaira repo's
    hook script path (resolved from this module's package location).

    Note: if the user moves khimaira or reinstalls via `uv sync --reinstall`,
    re-run `khimaira attach` to update the resolved paths.
    """
    # Python interpreter: project's venv
    python = project_path / ".venv" / "bin" / "python3"

    # Hook script: khimaira repo root / scripts / hooks / themis_pretool.py
    # This file is at packages/khimaira/src/khimaira/attach/settings_hooks.py
    # parents: [0]=attach/, [1]=khimaira/, [2]=src/, [3]=khimaira-pkg/, [4]=packages/, [5]=repo-root
    khimaira_root = Path(__file__).resolve().parents[5]
    hook_script = khimaira_root / "scripts" / "hooks" / "themis_pretool.py"

    return f"{python} {hook_script}"
