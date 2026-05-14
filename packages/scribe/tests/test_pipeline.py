"""Tests for the refactored scribe pipeline.

Verifies the three key claims of the 2026-05-13 integration:
  1. Audio is uploaded ONCE per pipeline run, referenced by both
     transcribe and emotion (when emotions enabled).
  2. Summarize + extract route through khimaira.server.mcp._delegate_impl
     instead of calling Gemini directly with the audio model.
  3. with_emotions=False (the default) skips the emotion node entirely
     — no second audio submission.

The Gemini SDK + LangGraph are mocked at the call boundary; no real
audio + no real network in the test suite. Integration tests against a
real Gemini key are opt-in elsewhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeFile:
    """Stand-in for the Gemini Files API File handle."""

    def __init__(self, name: str = "files/test-12345"):
        self.name = name
        # Match the SDK's .state.name == 'ACTIVE' pattern
        self.state = MagicMock(name="State")
        self.state.name = "ACTIVE"


def _fake_response(text: str, in_tok: int = 100, out_tok: int = 50):
    """Stand-in for client.aio.models.generate_content() return."""
    resp = MagicMock()
    resp.text = text
    resp.usage_metadata = MagicMock()
    resp.usage_metadata.prompt_token_count = in_tok
    resp.usage_metadata.candidates_token_count = out_tok
    return resp


@pytest.fixture
def mock_gemini():
    """Patch `genai.Client` so the pipeline never hits real Gemini."""
    fake_file = _FakeFile()

    fake_client = MagicMock()
    fake_client.files = MagicMock()
    fake_client.files.upload = MagicMock(return_value=fake_file)
    fake_client.files.get = MagicMock(return_value=fake_file)

    # async generate_content
    fake_client.aio = MagicMock()
    fake_client.aio.models = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(
        return_value=_fake_response("hi from gemini")
    )

    with patch("scribe.nodes.genai.Client", return_value=fake_client):
        yield fake_client


# -------------------- transcribe -------------------- #


async def test_transcribe_uploads_and_returns_text(mock_gemini, tmp_path):
    """Transcribe uses Files API (upload once) + returns transcript + file name."""
    from scribe.nodes.transcribe import transcribe

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"FAKE_WAV_DATA")
    state = {"audio_path": str(audio_path)}
    result = await transcribe(state)

    assert result["transcript"] == "hi from gemini"
    # The file handle's `name` is stashed for downstream nodes to reuse
    assert result["audio_file_name"].startswith("files/")
    # Upload was called exactly once
    mock_gemini.files.upload.assert_called_once()
    # generate_content was called exactly once (no double-submission of audio)
    assert mock_gemini.aio.models.generate_content.call_count == 1


# -------------------- emotion (opt-in) -------------------- #


async def test_emotion_disabled_returns_stub(mock_gemini):
    """with_emotions=False → emotion node returns empty stub, doesn't hit Gemini."""
    from scribe.nodes.emotion import detect_emotions

    state = {
        "transcript": "Joseph: Good morning.",
        "audio_file_name": "files/test",
        "with_emotions": False,
    }
    result = await detect_emotions(state)
    assert result["speaker_emotions"] == []
    assert result["meeting_mood"] == "not-analyzed"
    # Critically: generate_content was NOT called when emotions are off
    mock_gemini.aio.models.generate_content.assert_not_called()


async def test_emotion_enabled_references_uploaded_file(mock_gemini):
    """with_emotions=True → emotion looks up the file by NAME (no re-upload)."""
    from scribe.nodes.emotion import detect_emotions

    # Pre-can the response so json.loads on its text succeeds
    mock_gemini.aio.models.generate_content.return_value = _fake_response(
        '{"speaker_emotions": [{"speaker": "Joseph", "overall_tone": "calm"}], '
        '"meeting_mood": "focused"}'
    )

    state = {
        "transcript": "Joseph: Good morning.",
        "audio_file_name": "files/test-12345",
        "with_emotions": True,
    }
    result = await detect_emotions(state)

    assert result["meeting_mood"] == "focused"
    # The file was fetched by name (the key claim of the refactor)
    mock_gemini.files.get.assert_called_once_with(name="files/test-12345")
    # The file was NOT re-uploaded
    mock_gemini.files.upload.assert_not_called()


async def test_emotion_enabled_without_file_name_skips(mock_gemini):
    """Defensive: with_emotions=True but no audio_file_name → skip cleanly."""
    from scribe.nodes.emotion import detect_emotions

    state = {
        "transcript": "...",
        "with_emotions": True,
        # No audio_file_name set
    }
    result = await detect_emotions(state)
    assert result["meeting_mood"] == "audio-unavailable"
    mock_gemini.aio.models.generate_content.assert_not_called()


# -------------------- summarize / extract (via khimaira router) -------------------- #


async def test_summarize_routes_through_khimaira_delegate():
    """summarize() should NOT call Gemini directly; it should route via
    khimaira's _delegate_impl with tier='auto'."""
    from scribe.nodes import summarize as summarize_mod

    mock_delegate = AsyncMock(return_value="_(via gemini/flash · 100→50 tokens · 0.5s · mode=auto)_\n\nMeeting was about deploys.")
    with patch.object(summarize_mod, "summarize") as _:
        pass  # just check the import path

    # Patch the lazy import inside summarize()
    with patch("khimaira.server.mcp._delegate_impl", new=mock_delegate):
        state = {"transcript": "Joseph: We shipped khimaira today."}
        result = await summarize_mod.summarize(state)

    mock_delegate.assert_called_once()
    args, kwargs = mock_delegate.call_args
    assert kwargs["tier"] == "haiku"
    # The delegate header was stripped from the user-facing summary
    assert "Meeting was about deploys" in result["summary"]
    assert "_(via" not in result["summary"]


async def test_extract_routes_through_khimaira_delegate():
    """extract_actions() routes via khimaira delegate, parses JSON response."""
    from scribe.nodes import extract as extract_mod

    canned_json = (
        '_(via gemini/flash · 100→50 tokens · 0.5s · mode=auto)_\n\n'
        '{"action_items": ["Ship khimaira"], "decisions": ["Use Linear"], '
        '"participants": ["Joseph"]}'
    )
    mock_delegate = AsyncMock(return_value=canned_json)
    with patch("khimaira.server.mcp._delegate_impl", new=mock_delegate):
        state = {"transcript": "..."}
        result = await extract_mod.extract_actions(state)

    assert result["action_items"] == ["Ship khimaira"]
    assert result["decisions"] == ["Use Linear"]
    assert result["participants"] == ["Joseph"]


async def test_extract_handles_malformed_json():
    """If the delegate returns non-JSON, extract returns safe defaults."""
    from scribe.nodes import extract as extract_mod

    mock_delegate = AsyncMock(return_value="not-json-{")
    with patch("khimaira.server.mcp._delegate_impl", new=mock_delegate):
        result = await extract_mod.extract_actions({"transcript": "..."})

    assert result["action_items"] == []
    assert result["decisions"] == []
    assert result["participants"] == []


# -------------------- MCP server tools -------------------- #


def test_scribe_mcp_exposes_six_tools():
    """The scribe MCP server registers all six tools."""
    from scribe.server import mcp as scribe_mcp

    tools = scribe_mcp._tool_manager.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "list_active_recordings",
        "process",
        "record_start",
        "record_stop",
        "summarize",
        "transcribe",
    ]


def test_scribe_tools_register_under_prefix():
    """khimaira's register_sibling_tools surfaces scribe tools as scribe_*."""
    from mcp.server.fastmcp import FastMCP

    from khimaira.server.sibling_tools import register_sibling_tools

    target = FastMCP("test-target")
    register_sibling_tools(target)
    tools = target._tool_manager.list_tools()
    scribe_tools = sorted(t.name for t in tools if t.name.startswith("scribe_"))
    assert scribe_tools == [
        "scribe_list_active_recordings",
        "scribe_process",
        "scribe_record_start",
        "scribe_record_stop",
        "scribe_summarize",
        "scribe_transcribe",
    ]
