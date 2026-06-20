"""Unit tests for khimaira.hooks.session_end_utils.

Tests cover detect_domain (priority logic, keyword fallback, domain-lead
naming convention) and extract_transcript (JSONL parsing, truncation,
missing-file handling).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from khimaira.hooks.session_end_utils import (
    detect_domain,
    detect_project,
    extract_transcript,
)

# ---------------------------------------------------------------------------
# detect_domain
# ---------------------------------------------------------------------------


def test_detect_domain_backend_lead():
    assert detect_domain("backend-lead-1") == "backend"


def test_detect_domain_frontend_lead():
    assert detect_domain("jp-frontend-lead-2") == "frontend"


def test_detect_domain_data_lead():
    assert detect_domain("data-lead") == "data"


def test_detect_domain_devops_lead():
    assert detect_domain("acme-devops-lead-5") == "devops"


def test_detect_domain_no_lead_returns_general():
    assert detect_domain("khimaira-0") == "general"


def test_detect_domain_lead_suffix_wins_over_keyword_frequency():
    """Priority 1 (lead suffix) overrides frequency count.

    The name contains 50 occurrences of 'backend' in the transcript body
    but the name itself has 'frontend-lead', so frontend wins.
    """
    text = "frontend-lead-1 " + " ".join(["backend"] * 50)
    assert detect_domain(text) == "frontend"


def test_detect_domain_keyword_frequency_fallback():
    """No lead suffix → keyword with highest count wins."""
    text = "data data data backend devops"
    assert detect_domain(text) == "data"


def test_detect_domain_case_insensitive():
    assert detect_domain("BACKEND-LEAD") == "backend"


# ---------------------------------------------------------------------------
# detect_project — central manifest reverse-lookup
# ---------------------------------------------------------------------------


def _write_central_manifest(
    leads_dir: Path, project_name: str, root_path: Path
) -> None:
    """Write a minimal central manifest for detect_project tests."""
    content = (
        f'[project]\nname = "{project_name}"\nroot_path = "{root_path}"\n'
        f'[leads.backend]\npaths = ["packages/**"]\n'
    )
    (leads_dir / f"{project_name}.toml").write_text(content, encoding="utf-8")


def test_detect_project_matches_root_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """cwd inside root_path → correct project name returned."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    project_root = tmp_path / "myproject"
    project_root.mkdir()
    _write_central_manifest(leads_dir, "myproject", project_root)

    cwd = project_root / "packages" / "some" / "deep"
    cwd.mkdir(parents=True)
    assert detect_project(str(cwd)) == "myproject"


def test_detect_project_falls_back_to_cwd_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No matching central manifest → returns cwd basename."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    cwd = tmp_path / "some-project"
    cwd.mkdir()
    result = detect_project(str(cwd))
    assert result == "some-project"


def test_detect_project_longest_prefix_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Nested projects: manifest with longest (deepest) root_path wins."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    parent_root = tmp_path / "parent"
    child_root = tmp_path / "parent" / "child"
    parent_root.mkdir()
    child_root.mkdir()

    _write_central_manifest(leads_dir, "parent", parent_root)
    _write_central_manifest(leads_dir, "child", child_root)

    cwd = child_root / "src"
    cwd.mkdir()
    assert detect_project(str(cwd)) == "child"


def test_detect_project_no_false_sibling_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sibling dirs with shared prefix must NOT match (is_relative_to semantics)."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    khimaira_root = tmp_path / "khimaira"
    khimaira_root.mkdir()
    _write_central_manifest(leads_dir, "khimaira", khimaira_root)

    # sibling with a shared prefix — must NOT match as "khimaira"
    sibling = tmp_path / "khimaira-foo"
    sibling.mkdir()
    result = detect_project(str(sibling))
    assert result != "khimaira"
    assert result == "khimaira-foo"


def test_detect_project_falls_back_when_no_leads_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Absent central leads dir → cwd basename fallback."""
    empty_xdg = tmp_path / "empty_xdg"
    empty_xdg.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(empty_xdg))

    cwd = tmp_path / "myrepo"
    cwd.mkdir()
    assert detect_project(str(cwd)) == "myrepo"


def test_detect_project_qualified_key_khimaira(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """khimaira cwd → 'khimaira' (load-bearing: qualifies mnemosyne key as khimaira:backend)."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    khimaira_root = tmp_path / "khimaira"
    khimaira_root.mkdir()
    _write_central_manifest(leads_dir, "khimaira", khimaira_root)

    cwd = khimaira_root / "packages" / "khimaira" / "src"
    cwd.mkdir(parents=True)
    assert detect_project(str(cwd)) == "khimaira"


def test_detect_project_qualified_key_jeevy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """jeevy_portal cwd → 'jeevy' (load-bearing: qualifies mnemosyne key as jeevy:backend)."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    # jeevy uses jeevy_portal as directory name but project name "jeevy"
    jeevy_root = tmp_path / "jeevy_portal"
    jeevy_root.mkdir()
    _write_central_manifest(leads_dir, "jeevy", jeevy_root)

    cwd = jeevy_root / "apps" / "jeevy" / "src"
    cwd.mkdir(parents=True)
    # detect_project must return "jeevy" (from manifest name), NOT "jeevy_portal" (basename)
    assert detect_project(str(cwd)) == "jeevy"


# ---------------------------------------------------------------------------
# extract_transcript
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_extract_transcript_returns_none_for_unknown_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No JSONL file found → None."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    # Reload so _PROJECTS_ROOT picks up the new env var
    import importlib

    import khimaira.hooks.session_end_utils as mod

    importlib.reload(mod)
    from khimaira.hooks.session_end_utils import extract_transcript as et

    assert et("nonexistent-session-id") is None


def test_extract_transcript_reads_jsonl_via_path(tmp_path: Path):
    """transcript_path provided → reads directly, ignores project dirs."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    )
    result = extract_transcript("any-id", transcript_path=str(jsonl))
    assert result is not None
    assert "[user]: hello" in result
    assert "[assistant]: world" in result


def test_extract_transcript_handles_list_content(tmp_path: Path):
    """content as list of text blocks is supported."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "structured block"},
                    {"type": "tool_use", "id": "x"},
                ],
            }
        ],
    )
    result = extract_transcript("any-id", transcript_path=str(jsonl))
    assert result is not None
    assert "structured block" in result


def test_extract_transcript_truncates_to_max_chars(tmp_path: Path):
    """Transcripts exceeding max_chars are truncated with separator."""
    jsonl = tmp_path / "session.jsonl"
    long_text = "x" * 2000
    _write_jsonl(jsonl, [{"role": "user", "content": long_text}])
    result = extract_transcript("any-id", max_chars=100, transcript_path=str(jsonl))
    assert result is not None
    assert "...[truncated]..." in result
    assert len(result) <= 100 + len("\n...[truncated]...\n")


def test_extract_transcript_no_truncation_when_under_limit(tmp_path: Path):
    """Short transcripts are returned intact without separator."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [{"role": "user", "content": "short"}])
    result = extract_transcript("any-id", max_chars=50_000, transcript_path=str(jsonl))
    assert result is not None
    assert "...[truncated]..." not in result


def test_extract_transcript_returns_none_for_empty_jsonl(tmp_path: Path):
    """Empty JSONL (no parseable text content) → None."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("")
    result = extract_transcript("any-id", transcript_path=str(jsonl))
    assert result is None


def test_extract_transcript_returns_none_for_nonexistent_path(tmp_path: Path):
    """transcript_path points to nonexistent file → None."""
    result = extract_transcript(
        "any-id", transcript_path=str(tmp_path / "missing.jsonl")
    )
    assert result is None


# ---------------------------------------------------------------------------
# contiguous 600k window over-budget handling (task 4: bigger window, first/last-half)
# ---------------------------------------------------------------------------
# A decision-dense whole-session SELECTION was tried + reverted — chat-shaped fragments
# made the distiller continue the chat instead of distilling (0 pairs). The shipped
# behavior is a bigger CONTIGUOUS window with first-half+last-half truncation.


def test_over_budget_uses_contiguous_first_last_half(tmp_path: Path):
    """Over-budget → first-half + last-half contiguous slice (the …[truncated]… marker),
    NOT a reordered/gap-marked selection (which broke the distiller)."""
    jsonl = tmp_path / "s.jsonl"
    body = "x" * 2000
    _write_jsonl(jsonl, [{"role": "assistant", "content": body}])
    result = extract_transcript("id", max_chars=200, transcript_path=str(jsonl))
    assert result is not None
    assert "...[truncated]..." in result          # contiguous first/last-half path
    assert "…[gap]…" not in result                # no decision-dense gap markers
    assert len(result) <= 200 + len("\n...[truncated]...\n")


def test_under_budget_returned_whole(tmp_path: Path):
    """Under the window → full transcript, no truncation."""
    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [{"role": "assistant", "content": "a coherent narrative block"}])
    result = extract_transcript("id", max_chars=600_000, transcript_path=str(jsonl))
    assert result is not None
    assert "coherent narrative" in result
    assert "...[truncated]..." not in result
