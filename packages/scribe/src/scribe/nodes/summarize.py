"""Summarization node — routes through khimaira's pool router for cheapest text model.

Summarize is pure-text post-processing on the (small, ~3-10K token)
transcript. It doesn't need an audio-capable model. By routing
through `khimaira.dispatch._delegate_impl` with `tier="auto"`, the
pool router picks the cheapest model in our auto pool that covers
the `summarize` capability — typically gemini-2.5-flash-lite or
gemini-2.5-flash, much cheaper than the audio-capable model that
transcribe + emotion need.

Usage is recorded by khimaira's delegate path with `mode="auto"`,
so the scribe summarize call shows up in `khimaira usage savings`
alongside other auto-mode dispatches.
"""

from scribe.state import MeetingState


_PROMPT = (
    "You are a meeting summarizer. Given the following meeting transcript, "
    "provide a clear, structured summary.\n\n"
    "Include:\n"
    "1. **Meeting Overview** — 2-3 sentence high-level summary\n"
    "2. **Key Topics Discussed** — bullet points of main topics\n"
    "3. **Important Details** — any numbers, dates, names, or specifics mentioned\n\n"
    "Keep it concise but comprehensive.\n\n"
    "## Transcript\n\n"
)


async def summarize(state: MeetingState) -> dict:
    """Summarize the meeting transcript via khimaira's auto-router."""
    transcript = state["transcript"]

    try:
        from khimaira.server.mcp import _delegate_impl
    except ImportError:
        return {"summary": "[summary unavailable — khimaira.server not importable]"}

    # Pinned to tier="haiku" (claude) as a workaround for the
    # 2026-05-13 gemini-runner bug: gemini's `-p prompt` arg-passing
    # is broken — empty 0→0-token responses. Auto-routing currently
    # prefers gemini for text tasks (cheaper) but the call silently
    # fails. claude-haiku still under $1/M; the savings hit is tiny
    # compared to the previous "use opus everywhere" baseline. Switch
    # back to "auto" once tasks/gemini-runner-bug is resolved.
    result = await _delegate_impl(
        _PROMPT + transcript,
        tier="haiku",
        timeout_s=120,
        project=state.get("task_id") or "",
    )
    # The delegate result includes a "_(via runner/model · ... tokens · ...)_\n\n"
    # header. Strip it for the user-facing summary.
    if result.startswith("_(via "):
        try:
            result = result.split("\n\n", 1)[1]
        except IndexError:
            pass
    return {"summary": result}
