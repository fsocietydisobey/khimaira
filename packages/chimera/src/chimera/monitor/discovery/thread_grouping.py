"""Generic thread_id parser.

LangGraph projects use whatever convention they like for thread_ids:

  - bare UUIDs:                    "<uuid>"
  - simple namespace:              "<kind>:<uuid>"  (e.g. "orchestrator:<uuid>")
  - scoped with stages (jeevy):    "<kind>:<scope-id>:<stage>:<detail>"
                                   "deliverable:<dlv-uuid>:digestion:<run-uuid>"
                                   "deliverable:<dlv-uuid>:ingest:<source-id>"

This module parses a thread_id into four fields the UI groups by:

  scope_kind   — what kind of durable thing this thread belongs to
                 ("deliverable", "orchestrator", "thread", ...)
  scope_id     — the identifier of that thing (uuid, name, raw thread_id)
  stage        — the role / phase of this specific thread within its scope
                 ("ingest", "digestion", "output", "orchestrator", ...)
  stage_detail — extra discriminator when there are multiple threads of
                 the same stage within one scope (run UUID, source-id, ...)

The parser is intentionally heuristic — it covers the common shapes
without any per-project configuration. For projects that don't fit, a
later metadata-driven override path will replace this. UI code never
needs to know the conventions; it just groups by `scope_kind` /
`scope_id`, then sub-groups by `stage`.
"""

from __future__ import annotations

import re
from typing import TypedDict

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ThreadGrouping(TypedDict):
    scope_kind: str        # never empty — falls back to "thread"
    scope_id: str          # never empty — falls back to the raw thread_id
    stage: str             # never empty — falls back to scope_kind
    stage_detail: str      # may be empty


def is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def parse_grouping(thread_id: str) -> ThreadGrouping:
    """Parse a thread_id into UI-grouping fields. Always returns a dict —
    never raises, never returns None — so the caller doesn't need to
    special-case malformed ids."""
    if not thread_id:
        return {"scope_kind": "thread", "scope_id": "", "stage": "thread", "stage_detail": ""}

    parts = thread_id.split(":")

    # Pattern A: bare UUID — chimera-style chain runs
    if len(parts) == 1 and is_uuid(parts[0]):
        return {
            "scope_kind": "thread",
            "scope_id": parts[0],
            "stage": "thread",
            "stage_detail": "",
        }

    # Pattern B: <kind>:<scope-id>:<stage>:[<detail>]
    # The most common multi-stage pattern (jeevy's deliverable lifecycle).
    # We require parts[1] to look like an identifier worth grouping on
    # (UUID, integer, or other non-empty token).
    if len(parts) >= 3 and parts[1]:
        return {
            "scope_kind": parts[0] or "thread",
            "scope_id": parts[1],
            "stage": parts[2] or parts[0] or "thread",
            "stage_detail": ":".join(parts[3:]),
        }

    # Pattern C: <kind>:<id> — orchestrator-style top-level run
    if len(parts) == 2 and parts[1]:
        return {
            "scope_kind": parts[0] or "thread",
            "scope_id": parts[1],
            "stage": parts[0] or "thread",
            "stage_detail": "",
        }

    # Fallback — treat the whole thing as its own scope
    return {
        "scope_kind": parts[0] or "thread",
        "scope_id": thread_id,
        "stage": parts[0] or "thread",
        "stage_detail": "",
    }
