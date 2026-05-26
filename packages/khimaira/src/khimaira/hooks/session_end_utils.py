#!/usr/bin/env python3
"""Utilities for the khimaira session-end distillation hook.

Imported by session_end.py. May also be imported from the khimaira package
in test/tooling contexts.

Two public functions:
  detect_domain(session_name_or_transcript) -> str
  extract_transcript(session_id, max_chars, transcript_path) -> str | None
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_DOMAINS = ("backend", "frontend", "data", "devops")

_PROJECTS_ROOT = (
    Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
)


def _central_leads_dir() -> Path:
    """Stdlib-only resolution of the central leads directory.

    Follows XDG Base Directory Specification:
    ${XDG_DATA_HOME:-~/.local/share}/khimaira/leads/
    No imports from khimaira.leads — keeps this hook stdlib-only.
    """
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "khimaira" / "leads"


def detect_project(cwd: str) -> str:
    """Reverse-lookup cwd against central manifests; return the matching project name.

    Scans ~/.local/share/khimaira/leads/*.toml for a manifest whose root_path
    is an ancestor of cwd. Uses Path.is_relative_to() (Python 3.9+) for
    path-component-boundary matching — prevents false matches on sibling dirs
    with shared prefixes (e.g. /dev/khimaira vs /dev/khimaira-foo).

    Longest-match wins: if multiple manifests match (nested projects), the one
    with the deepest root_path (most path components) is chosen.

    Fallback: basename of cwd if no manifest matches or on any error.
    Stdlib only — no imports from khimaira.leads.
    """
    best_name: str | None = None
    best_depth = -1

    try:
        cwd_resolved = Path(cwd).resolve()
        leads_dir = _central_leads_dir()

        if not leads_dir.is_dir():
            raise FileNotFoundError("central leads dir absent")

        for toml_file in sorted(leads_dir.glob("*.toml")):
            try:
                text = toml_file.read_text(encoding="utf-8")
                name: str | None = None
                root_path_str: str | None = None
                for line in text.splitlines():
                    stripped = line.strip()
                    if name is None and stripped.startswith("name") and "=" in stripped:
                        name = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    if root_path_str is None and stripped.startswith("root_path") and "=" in stripped:
                        root_path_str = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    if name and root_path_str:
                        break

                if not name or not root_path_str:
                    continue

                root_resolved = Path(root_path_str).resolve()
                if cwd_resolved.is_relative_to(root_resolved):
                    depth = len(root_resolved.parts)
                    if depth > best_depth:
                        best_depth = depth
                        best_name = name
            except Exception:
                continue
    except Exception:
        pass

    if best_name:
        return best_name

    # Fallback: cwd basename
    try:
        return Path(cwd).resolve().name or "unknown"
    except Exception:
        return "unknown"


def detect_domain(session_name_or_transcript: str) -> str:
    """Segment-scan for domain keywords: backend, frontend, data, devops.

    Priority order:
    1. Explicit lead-role suffix in the input (e.g. "backend-lead-1" → "backend").
       Segment-based: split on '-' and check for "<domain>-lead" pair.
    2. Keyword frequency in the full text (for transcript content).

    Returns the best match, or "general" if no domain keyword found.
    """
    lower = session_name_or_transcript.lower()

    # Priority 1: explicit lead suffix — any "<domain>-lead" substring wins
    for domain in _DOMAINS:
        if f"{domain}-lead" in lower:
            return domain

    # Priority 2: keyword frequency
    counts = {d: lower.count(d) for d in _DOMAINS}
    max_count = max(counts.values())
    if max_count == 0:
        return "general"
    # Return the domain with the highest frequency; ties resolved by _DOMAINS order
    return max(_DOMAINS, key=lambda d: counts[d])


def extract_transcript(
    session_id: str,
    max_chars: int = 50_000,
    transcript_path: Optional[str] = None,
) -> Optional[str]:
    """Fetch session transcript text, truncated to max_chars.

    If transcript_path is given (from Stop hook payload), read it directly.
    Otherwise, search ~/.claude/projects/ subdirectories for {session_id}.jsonl.

    Truncation: keeps the first half + last half, with "...[truncated]..." separator.

    Returns None if the transcript is unavailable or empty.
    """
    if transcript_path:
        p = Path(transcript_path)
        if p.is_file():
            return _read_jsonl(p, max_chars)
        return None

    # Search all project dirs for the session JSONL
    if not _PROJECTS_ROOT.exists():
        return None
    for project_dir in _PROJECTS_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return _read_jsonl(candidate, max_chars)
    return None


def _read_jsonl(path: Path, max_chars: int) -> Optional[str]:
    """Parse a Claude Code session JSONL and extract readable text content."""
    parts: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                role = rec.get("role") or rec.get("type", "")
                content = rec.get("content") or (rec.get("message") or {}).get(
                    "content"
                )
                if isinstance(content, str) and content.strip():
                    parts.append(f"[{role}]: {content.strip()}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                parts.append(f"[{role}]: {text}")
    except OSError:
        return None

    if not parts:
        return None

    full = "\n".join(parts)
    return _truncate(full, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]
