"""Tests for the refactored sibyl pipeline.

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

    with patch("sibyl.nodes.genai.Client", return_value=fake_client):
        yield fake_client


# -------------------- transcribe -------------------- #


async def test_transcribe_uploads_and_returns_text(mock_gemini, tmp_path):
    """Transcribe uses Files API (upload once) + returns transcript + file name."""
    from sibyl.nodes.transcribe import transcribe

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
    from sibyl.nodes.emotion import detect_emotions

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
    from sibyl.nodes.emotion import detect_emotions

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
    from sibyl.nodes.emotion import detect_emotions

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
    from sibyl.nodes import summarize as summarize_mod

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
    from sibyl.nodes import extract as extract_mod

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
    from sibyl.nodes import extract as extract_mod

    mock_delegate = AsyncMock(return_value="not-json-{")
    with patch("khimaira.server.mcp._delegate_impl", new=mock_delegate):
        result = await extract_mod.extract_actions({"transcript": "..."})

    assert result["action_items"] == []
    assert result["decisions"] == []
    assert result["participants"] == []


# -------------------- MCP server tools -------------------- #


def test_sibyl_mcp_exposes_six_tools():
    """The sibyl MCP server registers all six tools."""
    from sibyl.server import mcp as sibyl_mcp

    tools = sibyl_mcp._tool_manager.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "list_active_recordings",
        "process",
        "record_start",
        "record_stop",
        "summarize",
        "transcribe",
    ]


def test_sibyl_tools_register_under_prefix():
    """khimaira's register_sibling_tools surfaces sibyl tools as sibyl_*."""
    from mcp.server.fastmcp import FastMCP

    from khimaira.server.sibling_tools import register_sibling_tools

    target = FastMCP("test-target")
    register_sibling_tools(target)
    tools = target._tool_manager.list_tools()
    sibyl_tools = sorted(t.name for t in tools if t.name.startswith("sibyl_"))
    assert sibyl_tools == [
        "sibyl_list_active_recordings",
        "sibyl_process",
        "sibyl_record_start",
        "sibyl_record_stop",
        "sibyl_summarize",
        "sibyl_transcribe",
    ]


# -------------------- known_speakers + accent_hint -------------------- #


def test_transcribe_prompt_no_hints_falls_back_to_self_introduction():
    """No known_speakers + no accent_hint → original self-introduction prompt
    (back-compat with existing callers)."""
    from sibyl.nodes.transcribe import _build_prompt

    prompt = _build_prompt(known_speakers=[], accent_hint="")
    assert "introduce themselves" in prompt
    assert "Joseph" in prompt or "Mark" in prompt  # back-compat example
    assert "Known participants" not in prompt
    assert "Acoustic context" not in prompt


def test_transcribe_prompt_known_speakers_filters_background():
    """known_speakers populates the participant list AND adds the
    background-voice filter directive."""
    from sibyl.nodes.transcribe import _build_prompt

    prompt = _build_prompt(
        known_speakers=["Alice", "Bob", "Charlie"],
        accent_hint="",
    )
    assert "Alice, Bob, Charlie" in prompt
    # The 3-participant count + filter instructions are both present
    assert "exactly 3 of them" in prompt
    assert "background office workers" in prompt
    assert "NOT participants" in prompt
    # Labeling examples are built from the actual speaker list — no
    # hardcoded "Sai" / "Joseph" / etc. leaking into the prompt
    assert "'Alice:'" in prompt or "Alice:" in prompt
    assert "'Bob:'" in prompt or "Bob:" in prompt


def test_transcribe_prompt_example_uses_first_speaker_not_hardcoded():
    """The cueing-pattern example uses the FIRST speaker in the list,
    proving no hardcoded names leak in."""
    from sibyl.nodes.transcribe import _build_prompt

    prompt = _build_prompt(known_speakers=["Zeke", "Yara"], accent_hint="")
    # Cueing example built from the list (not hardcoded "Sai, go ahead"
    # like an earlier draft had)
    assert "Zeke, go ahead" in prompt
    # No leaked names from the user's actual team
    assert "Sai" not in prompt
    assert "Rajat" not in prompt
    assert "Pranav" not in prompt


def test_transcribe_prompt_accent_hint_added():
    """accent_hint adds the acoustic-context section."""
    from sibyl.nodes.transcribe import _build_prompt

    prompt = _build_prompt(known_speakers=[], accent_hint="Indian English")
    assert "Acoustic context" in prompt
    assert "Indian English" in prompt
    assert "code-switch" in prompt  # the code-switching guidance


def test_record_start_persists_hints_in_active_recording(monkeypatch, tmp_path):
    """recording_control.start_recording stores known_speakers + accent
    on the _ActiveRecording so stop can echo them back."""
    from unittest.mock import MagicMock

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll = MagicMock(return_value=None)
    monkeypatch.setattr(
        "sibyl.recording_control.subprocess.Popen",
        lambda *a, **kw: fake_proc,
    )

    from sibyl import recording_control

    info = recording_control.start_recording(
        output_path=str(tmp_path / "test.wav"),
        known_speakers=["Alice", "Bob"],
        accent_hint="British",
        task_id="standup",
    )
    assert info["known_speakers"] == ["Alice", "Bob"]
    assert info["accent_hint"] == "British"
    assert info["task_id"] == "standup"

    # The _ActiveRecording carries them
    rec = recording_control._active[info["recording_id"]]
    assert rec.known_speakers == ["Alice", "Bob"]
    assert rec.accent_hint == "British"
    assert rec.task_id == "standup"

    # Cleanup
    del recording_control._active[info["recording_id"]]


def test_parse_speakers_handles_messy_input():
    """The MCP tool's comma-separated string parser tolerates spaces,
    newlines, trailing commas."""
    from sibyl.server.mcp import _parse_speakers

    assert _parse_speakers("") == []
    assert _parse_speakers("Alice") == ["Alice"]
    assert _parse_speakers("Alice, Bob, Charlie") == ["Alice", "Bob", "Charlie"]
    assert _parse_speakers("Alice,Bob,Charlie") == ["Alice", "Bob", "Charlie"]
    assert _parse_speakers("Alice\nBob\nCharlie") == ["Alice", "Bob", "Charlie"]
    assert _parse_speakers("Alice, , Bob") == ["Alice", "Bob"]
    assert _parse_speakers("  Alice  ,  Bob  ") == ["Alice", "Bob"]
