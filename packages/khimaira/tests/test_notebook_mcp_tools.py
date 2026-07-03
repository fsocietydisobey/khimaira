"""Tests for the notebook_* MCP tool family (khimaira.server.notebook_tools).

Thin async wrappers over the daemon's /api/notes* routes — same shape as
kg_*/session_* in monitor_tools.py. We mock `_get`/`_post`/`_patch` (the
daemon HTTP layer, already covered by test_monitor_tools) and assert:

  1. Happy path — formatted output carries the real data.
  2. A daemon error string passes through verbatim (no crash, no masking).
  3. Empty / no-hits produce a clear message, not a stack trace.
  4. project/tab/repo filters reach the right query param or client-side filter.
  5. Guard-rail inputs (empty note_id, empty query, empty resolution, no fields
     to update) are rejected before any HTTP call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from khimaira.server import notebook_tools as nt


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# notebook_list
# ---------------------------------------------------------------------------


def _note(**overrides) -> dict:
    base = {
        "id": "note-1",
        "title": "Fix the reaper race",
        "status": "processed",
        "lifecycle": "reviewed",
        "repo": "khimaira",
        "tab_id": "default",
    }
    base.update(overrides)
    return base


def test_notebook_list_happy_path():
    payload = {"notes": [_note()]}
    with patch.object(nt, "_get", return_value=payload) as mock_get:
        out = _run(nt.notebook_list())
    assert "1 note(s)" in out
    assert "note-1" in out
    assert "Fix the reaper race" in out
    assert "[reviewed]" in out
    mock_get.assert_called_once_with("/api/notes")


def test_notebook_list_tab_filter_passed_as_query_param():
    with patch.object(nt, "_get", return_value={"notes": []}) as mock_get:
        _run(nt.notebook_list(tab="t1"))
    assert mock_get.call_args[0][0] == "/api/notes?tab_id=t1"


def test_notebook_list_project_filter_applied_client_side():
    payload = {"notes": [_note(id="a", repo="khimaira"), _note(id="b", repo="jeevy_portal")]}
    with patch.object(nt, "_get", return_value=payload):
        out = _run(nt.notebook_list(project="jeevy_portal"))
    assert "`b`" in out
    assert "`a`" not in out


def test_notebook_list_empty_store():
    with patch.object(nt, "_get", return_value={"notes": []}):
        out = _run(nt.notebook_list())
    assert "no notes" in out.lower()


def test_notebook_list_error_passthrough():
    with patch.object(nt, "_get", return_value="khimaira-monitor → HTTP 500: boom"):
        out = _run(nt.notebook_list())
    assert "HTTP 500" in out


# ---------------------------------------------------------------------------
# notebook_search
# ---------------------------------------------------------------------------


def test_notebook_search_happy_path():
    payload = {"hits": [{"note_id": "n1", "score": 0.91}, {"note_id": "n2", "score": 0.7}]}
    with patch.object(nt, "_get", return_value=payload):
        out = _run(nt.notebook_search("session reaper race"))
    assert "2 match(es)" in out
    assert "`n1`" in out and "0.91" in out


def test_notebook_search_no_hits():
    with patch.object(nt, "_get", return_value={"hits": []}):
        out = _run(nt.notebook_search("nothing matches this"))
    assert "no notes match" in out.lower()


def test_notebook_search_rejects_empty_query():
    with patch.object(nt, "_get") as mock_get:
        out = _run(nt.notebook_search("   "))
    assert "❌" in out
    mock_get.assert_not_called()


def test_notebook_search_project_filter_reaches_query_string():
    with patch.object(nt, "_get", return_value={"hits": []}) as mock_get:
        _run(nt.notebook_search("race condition", project="jeevy_portal", top_k=3))
    called_path = mock_get.call_args[0][0]
    assert "top_k=3" in called_path
    assert "repo=jeevy_portal" in called_path


def test_notebook_search_error_passthrough():
    with patch.object(nt, "_get", return_value="khimaira-monitor → HTTP 404: no adapter"):
        out = _run(nt.notebook_search("q"))
    assert "HTTP 404" in out


# ---------------------------------------------------------------------------
# notebook_get
# ---------------------------------------------------------------------------


def test_notebook_get_happy_path_with_pipeline_and_resolution():
    payload = _note(
        raw_text="raw paste",
        pipeline={"summary": "s", "organized_md": "# md"},
        resolution="fixed it",
        resolved_by="agent-1",
        resolved_at="2026-07-03T00:00:00+00:00",
        lifecycle="resolved",
    )
    with patch.object(nt, "_get", return_value=payload):
        out = _run(nt.notebook_get("note-1"))
    assert "Fix the reaper race" in out
    assert "raw paste" in out
    assert "# md" in out
    assert "fixed it" in out
    assert "agent-1" in out


def test_notebook_get_no_resolution_prompts_write_back():
    payload = _note(raw_text="raw", pipeline=None, resolution="")
    with patch.object(nt, "_get", return_value=payload):
        out = _run(nt.notebook_get("note-1"))
    assert "No resolution yet" in out
    assert "notebook_add_resolution" in out


def test_notebook_get_rejects_empty_note_id():
    with patch.object(nt, "_get") as mock_get:
        out = _run(nt.notebook_get(""))
    assert "❌" in out
    mock_get.assert_not_called()


def test_notebook_get_error_passthrough():
    with patch.object(nt, "_get", return_value="khimaira-monitor → HTTP 404: No note with id"):
        out = _run(nt.notebook_get("no-such-note"))
    assert "HTTP 404" in out


# ---------------------------------------------------------------------------
# notebook_ask
# ---------------------------------------------------------------------------


def test_notebook_ask_happy_path():
    payload = {"answer": "The answer is 42.", "sources": ["n1"], "healed": []}
    with patch.object(nt, "_post", return_value=payload) as mock_post:
        out = _run(nt.notebook_ask("what is the answer"))
    assert "The answer is 42." in out
    assert "`n1`" in out
    assert "Healed" not in out
    assert mock_post.call_args[0][0] == "/api/notes/ask"
    assert mock_post.call_args[0][1] == {"question": "what is the answer"}


def test_notebook_ask_reports_healed_notes():
    payload = {"answer": "ok", "sources": ["n1"], "healed": ["n1"]}
    with patch.object(nt, "_post", return_value=payload):
        out = _run(nt.notebook_ask("q"))
    assert "Healed" in out
    assert "`n1`" in out


def test_notebook_ask_project_filter_included_in_body():
    with patch.object(
        nt, "_post", return_value={"answer": "", "sources": [], "healed": []}
    ) as mock_post:
        _run(nt.notebook_ask("q", project="jeevy_portal"))
    assert mock_post.call_args[0][1] == {"question": "q", "repo": "jeevy_portal"}


def test_notebook_ask_rejects_empty_question():
    with patch.object(nt, "_post") as mock_post:
        out = _run(nt.notebook_ask(""))
    assert "❌" in out
    mock_post.assert_not_called()


def test_notebook_ask_error_passthrough():
    with patch.object(nt, "_post", return_value="khimaira-monitor → HTTP 500: boom"):
        out = _run(nt.notebook_ask("q"))
    assert "HTTP 500" in out


# ---------------------------------------------------------------------------
# notebook_add_resolution
# ---------------------------------------------------------------------------


def test_notebook_add_resolution_happy_path():
    payload = _note(resolution="fixed it", resolved_by="agent-1", lifecycle="resolved")
    with patch.object(nt, "_post", return_value=payload) as mock_post:
        out = _run(nt.notebook_add_resolution("note-1", "fixed it", resolved_by="agent-1"))
    assert "resolved" in out.lower()
    assert "note-1" in out
    assert mock_post.call_args[0][0] == "/api/notes/note-1/resolution"
    assert mock_post.call_args[0][1] == {"resolution": "fixed it", "resolved_by": "agent-1"}


def test_notebook_add_resolution_rejects_empty_note_id():
    with patch.object(nt, "_post") as mock_post:
        out = _run(nt.notebook_add_resolution("", "fixed it"))
    assert "❌" in out
    mock_post.assert_not_called()


def test_notebook_add_resolution_rejects_empty_resolution():
    with patch.object(nt, "_post") as mock_post:
        out = _run(nt.notebook_add_resolution("note-1", "   "))
    assert "❌" in out
    mock_post.assert_not_called()


def test_notebook_add_resolution_error_passthrough():
    with patch.object(nt, "_post", return_value="khimaira-monitor → HTTP 404: No note with id"):
        out = _run(nt.notebook_add_resolution("no-such-note", "fixed it"))
    assert "HTTP 404" in out


# ---------------------------------------------------------------------------
# notebook_update
# ---------------------------------------------------------------------------


def test_notebook_update_happy_path_only_sends_changed_fields():
    payload = _note(title="renamed")
    with patch.object(nt, "_patch", return_value=payload) as mock_patch:
        out = _run(nt.notebook_update("note-1", title="renamed"))
    assert "updated" in out.lower()
    assert mock_patch.call_args[0][0] == "/api/notes/note-1"
    assert mock_patch.call_args[0][1] == {"title": "renamed"}


def test_notebook_update_multiple_fields():
    with patch.object(nt, "_patch", return_value=_note()) as mock_patch:
        _run(nt.notebook_update("note-1", status="promoted", repo="jeevy_portal"))
    assert mock_patch.call_args[0][1] == {"status": "promoted", "repo": "jeevy_portal"}


def test_notebook_update_rejects_no_fields():
    with patch.object(nt, "_patch") as mock_patch:
        out = _run(nt.notebook_update("note-1"))
    assert "❌" in out
    mock_patch.assert_not_called()


def test_notebook_update_rejects_empty_note_id():
    with patch.object(nt, "_patch") as mock_patch:
        out = _run(nt.notebook_update("", title="x"))
    assert "❌" in out
    mock_patch.assert_not_called()


def test_notebook_update_error_passthrough():
    with patch.object(nt, "_patch", return_value="khimaira-monitor → HTTP 422: Invalid status"):
        out = _run(nt.notebook_update("note-1", status="bogus"))
    assert "HTTP 422" in out
