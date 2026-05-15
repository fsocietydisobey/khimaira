"""Sibyl MCP tools — drive the meeting pipeline from any Claude Code session.

Tools (all surface under `mcp__khimaira__sibyl_*` via khimaira's
sibling-tools registry):

- `record_start(output_path?)` — start audio capture as a managed
  subprocess. Returns a recording_id the caller passes back to stop.
- `record_stop(recording_id)` — SIGINT the recorder, wait for the WAV
  file to materialize, return the path.
- `transcribe(audio_path)` — audio → text only. No summarize/extract.
- `process(audio_path, with_emotions=False)` — full pipeline.
  Transcribe → parallel(summarize + extract [+ emotions if enabled]).
  Per-node usage lands in khimaira's usage.jsonl with role tags.
- `summarize(transcript)` — text-only summary + actions + decisions
  on a transcript you already have. Routes through khimaira's auto
  pool router. No audio cost.

The pipeline is async; the MCP tool wrappers await the LangGraph
invocation directly. For long meetings (~hour+) the caller's MCP
timeout may matter — Gemini transcription of 60min audio runs ~30-60s
in our experience, well within Claude Code's defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sibyl import recording_control
from sibyl.graph import process_meeting
from sibyl.nodes.transcribe import transcribe as transcribe_node


mcp = FastMCP("sibyl")


def _parse_speakers(raw: str) -> list[str]:
    """Parse a comma- or newline-separated speaker list. MCP tools take
    string args (no native list type), so the caller passes
    'Alice, Bob, Charlie' and we split into ['Alice', 'Bob', 'Charlie'].
    Empty input → empty list."""
    if not raw or not raw.strip():
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


@mcp.tool()
async def record_start(
    output_path: str = "",
    known_speakers: str = "",
    accent_hint: str = "",
    task_id: str = "",
) -> str:
    """Start recording a meeting in the background.

    Captures system audio + microphone, mixed to a single WAV file.
    Returns a JSON-shaped status block with `recording_id`, `output_path`,
    and the transcription hints you declared. Pass the `recording_id` to
    `record_stop` when the meeting ends — `record_stop` echoes your hints
    back so the agent can pipe them into `sibyl_process` automatically.

    The participant names you supply are NEVER persisted by khimaira —
    they live only in-memory for the duration of the recording, get
    returned at stop time, and that's it. khimaira stays generic.

    Args:
        output_path: Optional WAV destination. Default:
            ~/.local/share/sibyl/meeting_<timestamp>.wav.
        known_speakers: Comma-separated participant names — e.g.
            "Alice, Bob, Charlie". When provided, transcribe filters
            voices that aren't on the list (background office workers,
            hallway chatter) and uses these names for labeling rather
            than generic "Speaker 1"/"Unknown Speaker". Empty = use
            today's "label by self-introduction" fallback.
        accent_hint: Optional acoustic context to prime Gemini's audio
            understanding (e.g. "Indian English", "British + American",
            "speakers may code-switch to Hindi"). Empty = no hint.
        task_id: Optional project label for khimaira usage attribution.
            Stored on the recording; flows through to per-node usage
            records when process runs.
    """
    speakers = _parse_speakers(known_speakers)
    info = recording_control.start_recording(
        output_path or None,
        known_speakers=speakers,
        accent_hint=accent_hint or "",
        task_id=task_id or "",
    )
    hints_block = ""
    if speakers:
        hints_block += f"  participants: {', '.join(speakers)}\n"
    if accent_hint:
        hints_block += f"  accent:       {accent_hint}\n"
    if task_id:
        hints_block += f"  task_id:      {task_id}\n"
    return (
        f"🎙️  Recording started.\n"
        f"  recording_id: {info['recording_id']}\n"
        f"  output_path:  {info['output_path']}\n"
        f"  pid:          {info['pid']}\n"
        f"  started_at:   {info['started_at']}\n"
        f"{hints_block}"
        f"\nCall `sibyl_record_stop(recording_id={info['recording_id']!r})` "
        f"when the meeting ends. Stop will echo your participant list + "
        f"accent so you can pipe them into `sibyl_process` without "
        f"retyping."
    )


@mcp.tool()
async def record_stop(recording_id: str) -> str:
    """Stop an in-flight recording and return the saved file path.

    SIGINTs the recorder subprocess (clean stop + save), waits up to
    10s for the WAV file to finalize. Echoes back the transcription
    hints (participant list, accent, task_id) the caller declared at
    start time so the agent can pipe them straight into `sibyl_process`.

    Args:
        recording_id: ID returned by `record_start`.
    """
    try:
        info = recording_control.stop_recording(recording_id)
    except ValueError as exc:
        return f"❌ {exc}"
    out = info["output_path"]
    size_mb = info.get("size_bytes", 0) / 1_000_000.0
    clean = "✅" if info.get("stopped_cleanly") else "⚠️"
    speakers = info.get("known_speakers") or []
    accent = info.get("accent_hint") or ""
    task = info.get("task_id") or ""

    # Build the recommended next-step process call with hints pre-filled
    # so the agent can paste it back without retyping anything.
    process_args = [f"audio_path={out!r}"]
    if speakers:
        process_args.append(f"known_speakers={', '.join(speakers)!r}")
    if accent:
        process_args.append(f"accent_hint={accent!r}")
    if task:
        process_args.append(f"task_id={task!r}")
    process_call = "sibyl_process(" + ", ".join(process_args) + ")"

    echoed = ""
    if speakers:
        echoed += f"  participants: {', '.join(speakers)}\n"
    if accent:
        echoed += f"  accent:       {accent}\n"

    return (
        f"{clean} Recording stopped.\n"
        f"  output_path:  {out}\n"
        f"  size:         {size_mb:.2f} MB\n"
        f"  started_at:   {info.get('started_at', '?')}\n"
        f"  stopped_at:   {info.get('stopped_at', '?')}\n"
        f"{echoed}"
        f"\nNext: `{process_call}` to transcribe + summarize."
    )


@mcp.tool()
async def transcribe(
    audio_path: str,
    known_speakers: str = "",
    accent_hint: str = "",
) -> str:
    """Transcribe an audio file (no summarize / extract).

    Uses Gemini's audio-capable model via the Files API. Returns the
    full transcript text.

    Args:
        audio_path: Path to a WAV / audio file.
        known_speakers: Comma-separated participant names. When set,
            transcribe filters background voices and labels with these
            names. See `sibyl_record_start` for the full semantics.
        accent_hint: Acoustic context (e.g. "Indian English").
    """
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        return f"❌ no audio file at {path}"
    state = {
        "audio_path": str(path),
        "with_emotions": False,
        "task_id": None,
        "known_speakers": _parse_speakers(known_speakers),
        "accent_hint": accent_hint or "",
    }
    result = await transcribe_node(state)
    return result.get("transcript", "(empty transcript)")


@mcp.tool()
async def process(
    audio_path: str,
    with_emotions: bool = False,
    task_id: str = "",
    known_speakers: str = "",
    accent_hint: str = "",
) -> str:
    """Run the full meeting pipeline on an audio file.

    Transcribe → (summarize + extract [+ emotions]) in parallel.
    Returns a JSON blob with summary, action_items, decisions,
    participants, transcript (and speaker_emotions + meeting_mood
    when with_emotions=True).

    Token-cost notes:
    - Audio is uploaded once via Files API (not duplicated per node).
    - Summarize + extract route through khimaira's pool router (text-only,
      cheapest competent model). Pinned to claude-haiku until the
      gemini-runner-bug fix lands.
    - Emotion is OPT-IN — leaving with_emotions=False keeps the second
      audio submission off entirely. Default is False; suitable for
      standups, retros, demos. Enable for performance reviews etc.

    Args:
        audio_path: Path to a WAV / audio file.
        with_emotions: Run emotion-detection pass (extra audio cost).
        task_id: Optional project label for khimaira usage attribution.
        known_speakers: Comma-separated participant names. When set,
            transcribe filters non-participant voices and uses these
            names for labeling rather than generic "Speaker N". khimaira
            never persists the list — it's caller-provided per call.
        accent_hint: Acoustic context (e.g. "Indian English") to prime
            Gemini's audio understanding.
    """
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        return f"❌ no audio file at {path}"
    final = await process_meeting(
        path,
        with_emotions=with_emotions,
        task_id=task_id or None,
        known_speakers=_parse_speakers(known_speakers),
        accent_hint=accent_hint or "",
    )
    # Trim transcript to a head/tail snippet for the MCP return; full
    # transcript is in the state dict but ~10-50KB is unwieldy inline.
    transcript = final.get("transcript", "")
    if len(transcript) > 4000:
        transcript_view = transcript[:2000] + "\n\n[...truncated...]\n\n" + transcript[-2000:]
    else:
        transcript_view = transcript
    payload = {
        "summary": final.get("summary", ""),
        "action_items": final.get("action_items", []),
        "decisions": final.get("decisions", []),
        "participants": final.get("participants", []),
        "transcript_preview": transcript_view,
        "transcript_chars": len(transcript),
        "audio_path": str(path),
    }
    if with_emotions:
        payload["speaker_emotions"] = final.get("speaker_emotions", [])
        payload["meeting_mood"] = final.get("meeting_mood", "")
    return json.dumps(payload, indent=2, ensure_ascii=False)


@mcp.tool()
async def summarize(transcript: str, task_id: str = "") -> str:
    """Summarize + extract from a transcript you already have.

    No audio required — pure-text. Routes through khimaira's auto pool
    router (cheapest competent text model). Returns JSON with summary,
    action_items, decisions, participants.

    Useful when you captured the transcript via Live mode, an external
    tool, or you want to re-process an existing transcript without
    re-running the audio pipeline.

    Args:
        transcript: Full meeting transcript text.
        task_id: Optional project label for usage attribution.
    """
    if not transcript.strip():
        return "❌ empty transcript"
    from sibyl.nodes.extract import extract_actions
    from sibyl.nodes.summarize import summarize as summarize_node

    state = {
        "audio_path": "",
        "transcript": transcript,
        "task_id": task_id or None,
        "with_emotions": False,
    }
    summary_result = await summarize_node(state)
    extract_result = await extract_actions(state)
    payload = {
        "summary": summary_result.get("summary", ""),
        "action_items": extract_result.get("action_items", []),
        "decisions": extract_result.get("decisions", []),
        "participants": extract_result.get("participants", []),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


@mcp.tool()
async def list_active_recordings() -> str:
    """List recordings currently in-flight (started via record_start
    but not yet stopped). Useful when you've lost track of recording_id."""
    active = recording_control.list_active_recordings()
    if not active:
        return "📭 no active recordings."
    lines = [f"🎙️  {len(active)} active recording(s):", ""]
    for r in active:
        lines.append(f"  • {r['recording_id']} — pid {r['pid']} — {r['output_path']}")
        lines.append(f"    started at {r['started_at']}")
    return "\n".join(lines)
