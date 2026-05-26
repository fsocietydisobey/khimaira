"""Integration tests for khimaira.hooks.session_end (Stop hook).

These tests exercise the hook's main() function directly — stdin payload
injection, mnemosyne POST behaviour, and fail-open error handling.

All external calls (daemon API + mnemosyne POST) are monkeypatched; no
real network traffic is produced.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import khimaira.hooks.session_end as hook_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    session_id: str = "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
    transcript_path: str | None = None,
) -> str:
    p: dict = {"session_id": session_id, "hook_event_name": "Stop"}
    if transcript_path is not None:
        p["transcript_path"] = transcript_path
    return json.dumps(p)


def _patch_stdin(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_general_domain_does_not_post(monkeypatch: pytest.MonkeyPatch):
    """Sessions without a domain-lead pattern must not POST to mnemosyne."""
    _patch_stdin(monkeypatch, _make_payload())
    # Stub daemon to return a generic session name
    monkeypatch.setattr(hook_mod, "_get_session_name", lambda sid: "khimaira-0")

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = hook_mod.main()

    assert result == 0
    mock_urlopen.assert_not_called()


def test_backend_lead_fires_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """backend-lead session posts {domain, transcript, session_slug} to mnemosyne."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [{"role": "user", "content": "backend work"}])
    _patch_stdin(monkeypatch, _make_payload(transcript_path=str(jsonl)))
    monkeypatch.setattr(hook_mod, "_get_session_name", lambda sid: "backend-lead-1")
    monkeypatch.setattr(
        "khimaira.hooks.session_end.detect_project", lambda cwd: "khimaira"
    )

    captured_body: list[bytes] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def _fake_urlopen(req, timeout=None):
        captured_body.append(req.data)
        return _FakeResp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = hook_mod.main()

    assert result == 0
    assert len(captured_body) == 1
    posted = json.loads(captured_body[0])
    assert posted["domain"] == "khimaira:backend"
    assert posted["session_slug"] == "backend-lead-1"
    assert "backend work" in posted["transcript"]


def test_connection_refused_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ConnectionRefusedError (mnemosyne not running) → exit 0, no crash."""
    import urllib.error

    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [{"role": "user", "content": "data work"}])
    _patch_stdin(monkeypatch, _make_payload(transcript_path=str(jsonl)))
    monkeypatch.setattr(hook_mod, "_get_session_name", lambda sid: "data-lead-2")

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = hook_mod.main()

    assert result == 0


def test_timeout_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Timeout posting to mnemosyne → exit 0, no crash."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [{"role": "user", "content": "devops work"}])
    _patch_stdin(monkeypatch, _make_payload(transcript_path=str(jsonl)))
    monkeypatch.setattr(hook_mod, "_get_session_name", lambda sid: "devops-lead-1")

    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = hook_mod.main()

    assert result == 0


def test_empty_stdin_exits_cleanly(monkeypatch: pytest.MonkeyPatch):
    """Empty stdin payload → exit 0 without posting."""
    _patch_stdin(monkeypatch, "")

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = hook_mod.main()

    assert result == 0
    mock_urlopen.assert_not_called()
