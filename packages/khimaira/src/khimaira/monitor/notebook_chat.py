"""Grimoire chat-model backend — per-record persistent conversational chat.

Replaces the two-button ANSWER/REVISE toolbar (Phase 3) with one persistent
chat per record (a study guide OR a regular note — see CHAT-UNIFY, 2026-07-04):
research + answer by default, and when the user asks for a change, the agent
produces an edit that AUTO-APPLIES (no confirm click — undo is via the
version-history snapshot every raw_text write already gets, per Phase 4).
Locked design: Joseph, decision e2fba504.

Storage: a JSON sidecar per record (`notebook/chats/<note_id>.json`), atomic
tmp+rename overwrite per mutation — mirroring notes.py's OWN note-body
storage convention (`_write_note_atomic`/`_read_note_file`) rather than
inventing a new append-only convention. A sidecar (NOT a `chat_history`
field on the note record itself) keeps every OTHER note-record read
(list_notes, get_note, the MCP notebook_get tool) from carrying a full chat
transcript it doesn't need — chat is a genuinely separate concern from the
record's own content/pipeline.

Reuses everything Phase 3/4 built: the async job+poll infra
(notebook_pipeline.create_job/complete_job/fail_job/track_job_task),
`_invoke_agentic_grounded` (now schema-parameterized so this module can pass
ChatTurnOutput instead of ResearchOutput), `splice_section` +
`reprocess_after_raw_text_change` (the edit-apply path — version-history +
reprocess fire automatically; organize is explicitly skipped per-edit, see
below), and the per-call config dir + transcript-verified grounding under
`_invoke_claude_agentic` itself.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from khimaira.log import get_logger
from khimaira.monitor import notebook_pipeline, notebook_retrieval, notes

log = get_logger("monitor.notebook_chat")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Storage — one JSON sidecar per guide, full-array atomic overwrite per
# mutation (append/clear/compact). A human sends one message and waits for
# the reply in practice, so the read-modify-write race a true concurrent
# double-send could hit is the same accepted-not-mitigated class the rest of
# this codebase's note-record writes already carry (no file locking
# anywhere in notes.py either) — not a new risk this module introduces.
# ---------------------------------------------------------------------------


def _chats_dir() -> Path:
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    return xdg / "khimaira" / "notebook" / "chats"


def _chat_path(note_id: str) -> Path:
    return _chats_dir() / f"{note_id}.json"


def get_chat_history(note_id: str) -> list[dict[str, Any]]:
    """Full chat history for a guide, oldest first. Empty list if none yet
    (never raises on a missing/corrupt sidecar — fail-open, matching this
    notebook's general posture toward derived/cache-shaped state)."""
    path = _chat_path(note_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _write_chat_history(note_id: str, history: list[dict[str, Any]]) -> None:
    _chats_dir().mkdir(parents=True, exist_ok=True)
    path = _chat_path(note_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_chat_messages(note_id: str, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    history = get_chat_history(note_id)
    history.extend(messages)
    _write_chat_history(note_id, history)
    return history


def clear_chat(note_id: str) -> dict[str, Any]:
    """Wipe a guide's chat history. Raises ValueError if note_id doesn't exist."""
    notes.get_note(note_id)
    _write_chat_history(note_id, [])
    return {"cleared": True}


def _new_user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content, "ts": _now_iso()}


def _new_system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content, "ts": _now_iso()}


def _format_chat_history_for_prompt(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(no prior messages)"
    lines: list[str] = []
    role_labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    for msg in history:
        lines.append(f"{role_labels.get(msg['role'], msg['role'])}: {msg['content']}")
        edit = msg.get("edit")
        if edit:
            scope = edit.get("section_anchor") or "(whole guide)"
            lines.append(f"  [applied an edit to {scope}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The per-turn agentic call — answer-vs-edit routing is the AGENT's own
# structured-output decision (like Claude Code), not a separate endpoint.
# ---------------------------------------------------------------------------


class ChatEdit(BaseModel):
    section_anchor: str | None = None
    new_text: str


class ChatTurnOutput(BaseModel):
    answer: str
    code_citations: list[str] = []
    web_citations: list[str] = []
    edit: ChatEdit | None = None


_CHAT_INSTRUCTION_TEMPLATE = (
    notebook_pipeline._GROUNDING_IMPERATIVE + " "
    "You are a conversational assistant scoped to ONE document (a study "
    "guide or a note) — a chat session about it, like Claude Code scoped to "
    "a single file. Research the ACTUAL codebase (Read/Grep/Glob under "
    "{repo_root}) and the live web (WebSearch/WebFetch) to ground your "
    "answers; verify anything checkable rather than trusting the "
    "document's existing prose. You do NOT have a tool to browse or fetch "
    "arbitrary other notes — OTHER NOTES below is everything from the rest "
    "of the notebook you'll get for this turn (auto-retrieved by semantic "
    "match against the user's message); if it's not there, say so rather "
    "than guessing at a note's contents.\n\n"
    "By default, just ANSWER the user's message — leave edit null. ONLY "
    "when the user explicitly asks you to change, fix, update, add to, or "
    "rewrite the document (or a specific section of it), populate edit with "
    "section_anchor and new_text: section_anchor names an EXISTING section "
    "from the document's own headings if the change is scoped to one "
    "section (must match a real heading's anchor — check the document "
    "below), or null for a whole-document rewrite (also use null if the "
    "document has no headings at all). new_text is the FULL replacement "
    "text for that scope (the section including its own heading line, or "
    "the entire document). Edits auto-apply immediately — do not propose a "
    "change unless the user actually asked for one.\n\n"
    "Output ONLY a JSON object, no prose, no markdown fence, with keys: "
    "answer (string — your reply to show the user), code_citations (array "
    'of "file:line" strings), web_citations (array of URL strings), edit '
    "(null, or an object with section_anchor and new_text).\n\n"
    "DOCUMENT:\n{guide}\n\n"
    "OTHER NOTES (semantically related to the user's latest message — may "
    "or may not be relevant; use if helpful, ignore if not):\n{related_notes}\n\n"
    "CONVERSATION SO FAR:\n{history}"
)

# Cost control: this retrieval + injection runs on EVERY chat turn (unlike
# answer_question's one-shot ask), and each related note's body can be a
# full organized_md — capped well below notebook_retrieval's own top_k=5
# default so a chatty back-and-forth doesn't compound the per-turn prompt
# size on top of the primary document + growing conversation history.
_CHAT_RELATED_NOTES_LIMIT = 3

_NO_RELATED_NOTES = "(none found)"


async def _related_notes_for_chat(
    note_id: str, message: str, repo: str
) -> tuple[str, list[str]]:
    """Deterministic retrieval-injection (2026-07-06, Joseph report): the
    per-record chat runs in a subprocess locked to zero MCP servers and
    only `--add-dir`s the CODE repo, never the notebook's own storage — so
    without this, the chat is architecturally blind to every note but the
    one it's scoped to (confirmed live: asked about a different note by id,
    the model correctly said it had no way to fetch it). Rather than open a
    live notebook-lookup tool to the subprocess (more flexible but pierces
    the deliberate MCP-free sandboxing chosen for cost/determinism), this
    reuses the SAME semantic search `answer_question` already runs for the
    notebook-wide ask and injects the top matches as prompt context — the
    model never decides what to fetch, matching this codebase's
    deterministic-first grounding pattern (see ai-engineering.md).

    Returns (formatted section text, note_ids actually included) so the
    caller can surface those ids as `sources` alongside the model's own
    code/web citations — same shape as answer_question's `sources` field.
    """
    try:
        hits = await notebook_retrieval.search_notes_async(message, repo=repo)
    except Exception:
        log.warning("notebook_chat: related-notes search failed for %s", note_id, exc_info=True)
        return _NO_RELATED_NOTES, []

    sections: list[str] = []
    included_ids: list[str] = []
    for hit in hits:
        other_id = hit["note_id"]
        if other_id == note_id:
            continue  # the primary document is already under DOCUMENT above
        try:
            other = notes.get_note(other_id)
        except ValueError:
            continue  # indexed but deleted since — skip, don't fail the turn
        pipeline = other.get("pipeline") or {}
        body = pipeline.get("organized_md") or pipeline.get("summary") or notes.llm_view(other)
        if not body:
            continue
        sections.append(f"### {other.get('title', other_id)} (id: {other_id})\n\n{body}")
        included_ids.append(other_id)
        if len(included_ids) >= _CHAT_RELATED_NOTES_LIMIT:
            break

    if not sections:
        return _NO_RELATED_NOTES, []
    return "\n\n---\n\n".join(sections), included_ids

# Sensitive notes (2026-07-04): appended to the instruction ONLY for a
# sensitive note's chat turn. Belt (tell the model not to bother proposing
# an edit it can't safely make) — the suspenders is the structural guard in
# run_chat_turn below, which never invokes _try_apply_edit for a sensitive
# note regardless of what the model returns.
_SENSITIVE_CHAT_ADDENDUM = (
    " This note is marked SENSITIVE — you are seeing a REDACTED copy with "
    "real secret values replaced by placeholders like ‹SECRET:kind#N›. "
    "You MUST NOT propose an edit (leave edit null even if asked to change "
    "something) — you cannot safely edit content you can't fully see. If "
    "asked to make a change, explain in your answer that edits are disabled "
    "for sensitive notes."
)


def _try_apply_edit(
    note_id: str, raw_text: str, edit_payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate + apply a chat-proposed edit. Returns the applied edit's
    {section_anchor, diff, applied_at} on success, or None if the edit was
    unusable (empty new_text, or a section_anchor that doesn't match any
    CURRENT heading) — auto-apply has no human in the loop to catch a bad
    edit, so this function is the safety net: skip rather than corrupt.

    Applies via notes.update_note(raw_text=) (snapshots version history —
    the undo mechanism) then reprocess_after_raw_text_change(skip_organize=
    True) — structuring still regenerates abstract/tags for the new
    content, but the organize-classification hook is deliberately skipped
    per edit (a chatty back-and-forth would otherwise fire one organize
    LLM call per edit; the periodic sweep re-checks placement eventually).
    """
    section_anchor = edit_payload.get("section_anchor")
    new_text = edit_payload.get("new_text") or ""
    if not new_text.strip():
        log.warning("notebook_chat: chat edit for %s had empty new_text — skipping apply", note_id)
        return None

    if section_anchor is not None:
        if not any(
            h["anchor"] == section_anchor for h in notebook_pipeline._scan_headings(raw_text)
        ):
            log.warning(
                "notebook_chat: chat edit for %s named unknown section_anchor %r — skipping apply",
                note_id,
                section_anchor,
            )
            return None
        try:
            new_raw_text = notebook_pipeline.splice_section(raw_text, section_anchor, new_text)
        except ValueError as exc:
            log.warning("notebook_chat: splice failed for %s: %s", note_id, exc)
            return None
    else:
        new_raw_text = new_text

    diff = "\n".join(
        difflib.unified_diff(
            raw_text.splitlines(),
            new_raw_text.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )

    notes.update_note(note_id, raw_text=new_raw_text)
    notebook_pipeline.reprocess_after_raw_text_change(note_id, skip_organize=True)

    return {"section_anchor": section_anchor, "diff": diff, "applied_at": _now_iso()}


async def run_chat_turn(
    note_id: str,
    message: str,
    *,
    max_budget_usd: float = notebook_pipeline._AGENTIC_DEFAULT_BUDGET_USD,
) -> dict[str, Any]:
    """One chat turn: load history, run the agentic call grounded in the
    record (guide or note) + codebase + web, auto-apply an edit if the agent
    proposed one, append both messages to the persistent history.

    CHAT-UNIFY (2026-07-04): chat is no longer guide-only — any note kind
    works (a note is just a shorter, less-structured record than a guide;
    the chat loop already operates on llm_view(record) + the sidecar store +
    _invoke_agentic_grounded, none of which are guide-specific). Raises
    ValueError only if note_id doesn't exist (mirrors get_note).
    """
    record = notes.get_note(note_id)

    repo = record.get("repo") or "khimaira"
    repo_root = None if repo == notes.GENERAL_REPO else notebook_pipeline._repo_root(repo)
    history = get_chat_history(note_id)
    related_notes_section, related_note_ids = await _related_notes_for_chat(note_id, message, repo)

    instruction = _CHAT_INSTRUCTION_TEMPLATE.format(
        repo_root=repo_root or "(no codebase — general/cross-cutting note)",
        guide=notes.llm_view(record),
        related_notes=related_notes_section,
        history=_format_chat_history_for_prompt(history),
    )
    if record.get("sensitive"):
        instruction += _SENSITIVE_CHAT_ADDENDUM

    result = await notebook_pipeline._invoke_agentic_grounded(
        message,
        instruction,
        repo_root=repo_root,
        max_budget_usd=max_budget_usd,
        schema=ChatTurnOutput,
        target_repo=repo,
    )

    applied_edit: dict[str, Any] | None = None
    edit_payload = result.get("edit")
    if edit_payload and record.get("sensitive"):
        # Structural guard, not prompt-enforced: even if the model ignores
        # _SENSITIVE_CHAT_ADDENDUM and proposes an edit anyway, _try_apply_edit
        # is never invoked for a sensitive note — auto-apply has no human
        # gate, and the model only ever saw a redacted copy, so any edit it
        # produced can't safely be spliced back into the real content.
        log.info(
            "notebook_chat: chat edit for sensitive note %s was proposed but "
            "suppressed — auto-apply is disabled on sensitive notes",
            note_id,
        )
    elif edit_payload:
        applied_edit = _try_apply_edit(note_id, record["raw_text"], edit_payload)

    grounding = {
        "web_grounded": result["web_grounded"],
        "web_grounding_unverified": result["web_grounding_unverified"],
        "code_citations": result.get("code_citations", []),
        "web_citations": result.get("web_citations", []),
    }
    user_msg = _new_user_message(message)
    assistant_msg = {
        "role": "assistant",
        "content": result["answer"],
        "ts": _now_iso(),
        "edit": applied_edit,
        "cost": result.get("total_cost_usd"),
        "grounding": grounding,
        # Other notes retrieval-injected into this turn's prompt (2026-07-06)
        # — same field name/shape as answer_question's `sources`, so the
        # existing frontend citation rendering picks it up with no changes.
        "sources": related_note_ids,
    }
    append_chat_messages(note_id, user_msg, assistant_msg)

    return {
        "message": {"content": assistant_msg["content"], "edit": applied_edit},
        "grounding": grounding,
        "total_cost_usd": result.get("total_cost_usd"),
        "sources": related_note_ids,
    }


async def _run_chat_turn_job(
    job_id: str, note_id: str, message: str, max_budget_usd: float
) -> None:
    try:
        result = await run_chat_turn(note_id, message, max_budget_usd=max_budget_usd)
        notebook_pipeline.complete_job(job_id, kind="chat", **result)
    except ValueError as exc:
        notebook_pipeline.fail_job(job_id, kind="chat", error=str(exc))
    except Exception as exc:
        log.exception("notebook_chat: chat turn job %s crashed", job_id)
        notebook_pipeline.fail_job(job_id, kind="chat", error=str(exc))


def schedule_chat_turn(
    note_id: str,
    message: str,
    *,
    max_budget_usd: float = notebook_pipeline._AGENTIC_DEFAULT_BUDGET_USD,
) -> str:
    """Fire a chat turn as a background job (reusing notebook_pipeline's
    generic job store) — returns a job_id immediately; poll via
    notebook_pipeline.get_research_job(job_id) (kind="chat").

    Validates note_id exists BEFORE scheduling — fails fast with the same
    contract schedule_research_answer/revise already have, rather than
    handing back a job_id guaranteed to error. CHAT-UNIFY (2026-07-04): no
    longer guide-only — any note kind is a valid chat target.
    """
    notes.get_note(note_id)  # fail fast on an unknown note_id
    job_id = notebook_pipeline.create_job("chat")
    task = asyncio.create_task(_run_chat_turn_job(job_id, note_id, message, max_budget_usd))
    notebook_pipeline.track_job_task(task)
    return job_id


# ---------------------------------------------------------------------------
# Compact — load-bearing for cost control, not cosmetic: every turn passes
# the FULL history into the (expensive) agentic call, so unbounded history
# growth means unbounded per-turn cost. Summarizes older turns into ONE
# system message, keeping the tail verbatim.
# ---------------------------------------------------------------------------

_COMPACT_KEEP_TAIL = 4

_COMPACT_INSTRUCTION = (
    "Summarize the following chat conversation about a study guide into a "
    "concise paragraph capturing the key questions asked, answers given, "
    "and any edits made (name the sections). This summary will REPLACE the "
    "full history to save context for future turns — keep it factual and "
    "information-dense, not conversational. Output ONLY the summary text, "
    "no JSON, no markdown fence, no preamble."
)


async def compact_chat_history(note_id: str) -> dict[str, Any]:
    """Summarize older chat turns into ONE system message, keeping the last
    _COMPACT_KEEP_TAIL messages verbatim. Raises ValueError if note_id
    doesn't exist. A no-op (compacted=False) if there's nothing to compact
    yet."""
    record = notes.get_note(note_id)  # fail fast on an unknown note_id
    history = get_chat_history(note_id)
    if len(history) <= _COMPACT_KEEP_TAIL:
        return {"compacted": False, "message_count": len(history)}

    to_summarize = history[:-_COMPACT_KEEP_TAIL]
    tail = history[-_COMPACT_KEEP_TAIL:]
    transcript = _format_chat_history_for_prompt(to_summarize)

    summary_text = await notebook_pipeline._invoke_claude(
        transcript, _COMPACT_INSTRUCTION, target_repo=record.get("repo")
    )
    summary_message = _new_system_message(
        f"[Earlier conversation summarized] {summary_text.strip()}"
    )
    new_history = [summary_message, *tail]
    _write_chat_history(note_id, new_history)
    return {"compacted": True, "message_count": len(new_history)}
