"""Resolved notes -> mnemosyne training data (the roster-loop write-back).

A note earns training status by being worked to completion: Joseph pastes a
problem, master + the agent roster work it via the `notebook_*` MCP tools,
and write a RESOLUTION back (`notes.add_resolution`). That {problem,
resolution} pair is exactly the shape mnemosyne's distiller wants — this
module derives the pair and ships it to the local distillation service.

Mirrors notebook_retrieval.py's fire-and-forget discipline: the HTTP call to
mnemosyne is synchronous (stdlib urllib, see khimaira.hooks.mnemosyne_client)
and must never block an API route's response, so callers use
`schedule_promote` from an async route handler the same way they already use
`notebook_retrieval.schedule_upsert`. Fail-open everywhere — mnemosyne being
unreachable must never break saving a resolution; it just means training
doesn't fire for that note (logged, not raised).
"""

from __future__ import annotations

import asyncio
from typing import Any

from khimaira.log import get_logger

log = get_logger("monitor.notebook_training")

# Strong references to background promote tasks — asyncio.create_task() only
# holds a weak ref, so a fire-and-forget task can be silently garbage-collected
# mid-flight (same pattern as notebook_pipeline.py / notebook_retrieval.py).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def training_domain(record: dict[str, Any]) -> str:
    """`<repo>:notes` — one distillation domain per repo, so a resolved note
    about jeevy_portal doesn't get baked into the khimaira oracle and vice
    versa. NOTE: this recomputes from the note's live `repo` field rather
    than trusting `record["training"]["domain"]`, which `add_note` currently
    hardcodes to "khimaira:notes" regardless of the note's actual repo — a
    pre-existing v1 mismatch for non-khimaira notes, out of scope here."""
    repo = record.get("repo") or "khimaira"
    return f"{repo}:notes"


def build_training_pair(record: dict[str, Any]) -> dict[str, str]:
    """Derive {instruction, response} from a resolved note.

    instruction = the note's problem framing — title + the structured
    summary if the pipeline ran, else the raw paste (a note can be resolved
    before/without ever being structured). response = the resolution verbatim.
    """
    title = record.get("title") or ""
    pipeline = record.get("pipeline") or {}
    summary = pipeline.get("summary") or record.get("raw_text") or ""
    instruction = f"{title}\n\n{summary}".strip() if title else summary.strip()
    return {"instruction": instruction, "response": record.get("resolution") or ""}


def _format_transcript(pair: dict[str, str]) -> str:
    """mnemosyne's /distill endpoint takes free-form `transcript` text (see
    harvest_approval.py's curated_text convention) — not a structured pair.
    Format the pair as a simple instruction/response block."""
    return f"[Notebook resolution]\n\nProblem:\n{pair['instruction']}\n\nResolution:\n{pair['response']}"


def promote_resolved(record: dict[str, Any]) -> dict | None:
    """Send a resolved note's {problem, resolution} pair to mnemosyne.

    No-op (returns None, logs) if the note has no resolution yet — this is
    the training-quality gate: only resolved notes reach the distiller.
    Fail-open on any distill-client failure (mnemosyne_client.distill already
    fails open; the broad except here also covers import/attribute errors so
    this NEVER breaks the resolution write path that calls it).

    Sensitive notes (2026-07-04) are HARD-excluded here — this is the ONE
    choke point every training-export trigger funnels through (the only
    other producer, `notes.promote_note`, has its own guard that raises
    instead of silently skipping, since that's an explicit human action).
    A sensitive note's content must never bake into oracle training data.
    """
    if record.get("sensitive"):
        log.info(
            "notebook_training: %s is a sensitive note — hard-excluded from training export",
            record.get("id"),
        )
        return None
    if not record.get("resolution"):
        log.info("notebook_training: %s has no resolution yet — skipping distill", record.get("id"))
        return None
    try:
        from khimaira.hooks.mnemosyne_client import distill

        pair = build_training_pair(record)
        domain = training_domain(record)
        note_id = record.get("id", "")
        result = distill(domain, _format_transcript(pair), f"notebook-{note_id}")
        if result is None:
            log.warning(
                "notebook_training: distill unreachable/failed for note %s (domain=%s) — "
                "resolution saved, training did not fire",
                note_id,
                domain,
            )
        else:
            log.info("notebook_training: distilled note %s into domain=%s", note_id, domain)
        return result
    except Exception as exc:  # noqa: BLE001 — must never break the resolution write path
        log.warning("notebook_training: promote_resolved(%s) failed: %s", record.get("id"), exc)
        return None


def schedule_promote(record: dict[str, Any]) -> None:
    """Fire-and-forget promote off the event loop. Call from an async route
    handler right after a resolution is saved — the mnemosyne HTTP call is
    synchronous and must never block the response (mirrors
    notebook_retrieval.schedule_upsert)."""
    task = asyncio.create_task(asyncio.to_thread(promote_resolved, record))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
