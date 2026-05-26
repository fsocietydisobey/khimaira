"""Unit tests for khimaira.hooks.session_end_utils.

Tests cover detect_domain (priority logic, keyword fallback, domain-lead
naming convention) and extract_transcript (JSONL parsing, truncation,
missing-file handling).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from khimaira.hooks.session_end_utils import detect_domain, extract_transcript


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
# extract_transcript
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_extract_transcript_returns_none_for_unknown_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    _write_jsonl(jsonl, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    result = extract_transcript("any-id", transcript_path=str(jsonl))
    assert result is not None
    assert "[user]: hello" in result
    assert "[assistant]: world" in result


def test_extract_transcript_handles_list_content(tmp_path: Path):
    """content as list of text blocks is supported."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "structured block"},
                {"type": "tool_use", "id": "x"},
            ],
        }
    ])
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
    result = extract_transcript("any-id", transcript_path=str(tmp_path / "missing.jsonl"))
    assert result is None
