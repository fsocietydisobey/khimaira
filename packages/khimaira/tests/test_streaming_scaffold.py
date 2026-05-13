"""Tests for the streaming scaffold (#55 partial).

NORTH_STAR Phase 4 includes "streaming responses through delegate/auto."
This file covers the FOUNDATION that real per-chunk streaming will
build on:

  - `StreamChunk` dataclass — the unit of a stream
  - `CLIRunner.stream()` Protocol method — uniform API across runners
  - `default_stream_via_run` — degenerate one-chunk impl for runners
    without a native streaming mode

Real per-chunk streaming (parsing Claude's `--output-format stream-json`
line-by-line and yielding text-deltas as they arrive) is a separate
task. It can drop in without changing callers because they already
code against the Protocol.

Tests:
  - StreamChunk default values
  - default_stream_via_run yields exactly one final chunk that mirrors
    the runner's run() return
  - ClaudeRunner.stream() uses the default; yields one final chunk
  - The scaffolded stream() can be drop-in replaced (mock the runner's
    run() to fail; verify stream() surfaces the same failure shape)
"""

from __future__ import annotations

import pytest

from khimaira.dispatch.runners.base import (
    RunnerResult,
    StreamChunk,
    default_stream_via_run,
)


def test_stream_chunk_defaults():
    """A bare StreamChunk has empty text and is not marked final."""
    c = StreamChunk()
    assert c.text == ""
    assert c.is_final is False
    assert c.model == ""
    assert c.input_tokens == 0


def test_stream_chunk_final():
    """Final chunk carries the metadata."""
    c = StreamChunk(
        text="full answer",
        is_final=True,
        model="claude-haiku-4-5",
        input_tokens=100,
        output_tokens=50,
        session_id="sess-1",
    )
    assert c.is_final is True
    assert c.model == "claude-haiku-4-5"
    assert c.input_tokens == 100


async def test_default_stream_via_run_yields_one_final_chunk():
    """Runners without native streaming get a degenerate one-chunk
    stream that mirrors their run() return."""

    class _FakeRunner:
        name = "fake"

        def is_available(self):
            return True

        async def run(self, prompt, **kwargs):
            return RunnerResult(
                text="hello world",
                runner="fake",
                model="fake-model-v1",
                input_tokens=5,
                output_tokens=10,
                latency_s=0.1,
                session_id="s-1",
            )

    chunks: list[StreamChunk] = []
    async for chunk in default_stream_via_run(_FakeRunner(), "prompt"):
        chunks.append(chunk)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.is_final is True
    assert chunk.text == "hello world"
    assert chunk.model == "fake-model-v1"
    assert chunk.input_tokens == 5
    assert chunk.output_tokens == 10
    assert chunk.session_id == "s-1"


async def test_claude_runner_streams_per_chunk(monkeypatch):
    """ClaudeRunner.stream() now does REAL per-chunk streaming — parses
    Claude Code's `--output-format stream-json` events and yields one
    StreamChunk per content_block_delta + one final chunk with totals.

    We mock asyncio.create_subprocess_exec to return a canned stream
    of stream-json events captured from a real claude run."""
    from unittest.mock import AsyncMock, patch

    from khimaira.dispatch.runners.claude import ClaudeRunner

    # Real shape from a 2026-05-13 probe (trimmed):
    canned_stdout_lines = [
        b'{"type":"system","subtype":"init","session_id":"s-1","model":"claude-opus-4-7"}\n',
        b'{"type":"stream_event","event":{"type":"message_start","message":{"model":"claude-haiku-4-5","id":"msg-1"}},"session_id":"s-1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},"session_id":"s-1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"4"}},"session_id":"s-1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"."}},"session_id":"s-1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_stop","index":0},"session_id":"s-1"}\n',
        b'{"type":"stream_event","event":{"type":"message_stop"},"session_id":"s-1"}\n',
        b'{"type":"result","subtype":"success","is_error":false,"result":"4.","session_id":"s-1","usage":{"input_tokens":5,"output_tokens":2,"cache_creation_input_tokens":100,"cache_read_input_tokens":0}}\n',
    ]

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return b""

    class _FakeStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(canned_stdout_lines)
            self.stdin = _FakeStdin()
            self.returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    runner = ClaudeRunner()
    # Force is_available so we don't depend on real `gh` on PATH for this test.
    monkeypatch.setattr(ClaudeRunner, "is_available", lambda self: True)

    with patch(
        "khimaira.dispatch.runners.claude.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_FakeProc()),
    ):
        chunks: list[StreamChunk] = []
        async for chunk in runner.stream("what is 2+2?"):
            chunks.append(chunk)

    # Two delta chunks ("4" then ".") plus one final
    text_chunks = [c for c in chunks if not c.is_final]
    final = [c for c in chunks if c.is_final]
    assert len(text_chunks) == 2
    assert text_chunks[0].text == "4"
    assert text_chunks[1].text == "."
    assert "".join(c.text for c in text_chunks) == "4."

    assert len(final) == 1
    f = final[0]
    assert f.is_final
    assert f.model == "claude-haiku-4-5"
    # input_tokens folds in cache_creation + cache_read
    assert f.input_tokens == 5 + 100 + 0
    assert f.output_tokens == 2
    assert f.session_id == "s-1"


async def test_claude_runner_stream_handles_error_result(monkeypatch):
    """If claude emits a result event with is_error=true, the stream
    surfaces an error chunk and STILL emits a final chunk — non-fatal."""
    from unittest.mock import AsyncMock, patch

    from khimaira.dispatch.runners.claude import ClaudeRunner

    canned_lines = [
        b'{"type":"system","subtype":"init","session_id":"s-1"}\n',
        b'{"type":"result","subtype":"error","is_error":true,"result":"rate limit exceeded","session_id":"s-1","usage":{}}\n',
    ]

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class _FakeStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(canned_lines)
            self.stdin = _FakeStdin()
            self.returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    runner = ClaudeRunner()
    monkeypatch.setattr(ClaudeRunner, "is_available", lambda self: True)

    with patch(
        "khimaira.dispatch.runners.claude.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_FakeProc()),
    ):
        chunks: list[StreamChunk] = []
        async for chunk in runner.stream("anything"):
            chunks.append(chunk)

    # We get an error text chunk + a final chunk (non-fatal — stream
    # completes cleanly so callers don't have to handle two exception
    # paths).
    text_chunks = [c for c in chunks if not c.is_final]
    final = [c for c in chunks if c.is_final]
    assert len(text_chunks) == 1
    assert "rate limit exceeded" in text_chunks[0].text
    assert len(final) == 1


async def test_claude_runner_stream_skips_malformed_lines(monkeypatch):
    """Malformed JSON lines in the stream → skipped silently."""
    from unittest.mock import AsyncMock, patch

    from khimaira.dispatch.runners.claude import ClaudeRunner

    canned_lines = [
        b"not-json-{\n",
        b"\n",
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}}\n',
        b'{"type":"result","is_error":false,"result":"hi","session_id":"s","usage":{}}\n',
    ]

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class _FakeStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(canned_lines)
            self.stdin = _FakeStdin()
            self.returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    runner = ClaudeRunner()
    monkeypatch.setattr(ClaudeRunner, "is_available", lambda self: True)

    with patch(
        "khimaira.dispatch.runners.claude.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_FakeProc()),
    ):
        chunks = []
        async for c in runner.stream("hi"):
            chunks.append(c)

    text = "".join(c.text for c in chunks if not c.is_final)
    assert text == "hi"


async def test_stream_propagates_runner_exceptions():
    """If the underlying run() raises, the stream propagates the
    exception (caller's responsibility to handle)."""

    class _BoomRunner:
        name = "boom"

        def is_available(self):
            return True

        async def run(self, prompt, **kwargs):
            raise RuntimeError("simulated runner crash")

    runner = _BoomRunner()
    with pytest.raises(RuntimeError, match="simulated runner crash"):
        async for _ in default_stream_via_run(runner, "anything"):
            pass


async def test_concatenated_chunks_equal_run_text_for_degenerate_stream():
    """The contract: concatenating every chunk's `text` for a streamed
    response equals the `text` you'd have gotten from `run()`. Trivially
    true for the degenerate one-chunk stream — locks the contract so
    future real-streaming impls preserve it."""

    class _FakeRunner:
        name = "fake"

        def is_available(self):
            return True

        async def run(self, prompt, **kwargs):
            return RunnerResult(
                text="The quick brown fox jumps over the lazy dog.",
                runner="fake",
                model="m",
            )

    chunks = []
    async for c in default_stream_via_run(_FakeRunner(), ""):
        chunks.append(c)

    concatenated = "".join(c.text for c in chunks)
    assert concatenated == "The quick brown fox jumps over the lazy dog."
