"""One-off backfill: materialize member_roles for v1-era chats.

Usage (from khimaira repo root):
    .venv/bin/python3 packages/khimaira/scripts/backfill_member_roles.py

Context: chats created before Phase B v2 (member_roles materialization) have
member_roles=None in room.meta. _threshold_for_session's canonical lookup
(path 1) always misses, falling back to name-inference (path 2). The fallback
only works if the session's status.name is set at probe-registration time —
a race that causes Pattern 5 to fire at 90s (default) instead of the
role-specific threshold (e.g. architect=180s, verifier=300s, critic=120s).

This script appends a META record with an explicit member_roles dict to each
target chat, closing the race for all future probe registrations.

Scope: chat-dfa8121d87b9 only (ctx-pattern5-architect-threshold-misfire).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add src to path so we can import khimaira
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from khimaira.monitor.chats import (  # noqa: E402
    _append,
    _new_event_id,
    _now_iso,
    META,
    load_room,
)

CHAT_ID = "chat-dfa8121d87b9"

# Role assignments for chat-dfa8121d87b9 accepted members.
# Roles that have custom thresholds in _REPLY_OVERDUE_BY_ROLE:
#   architect=180s, analyst=180s, verifier=300s, critic=120s
# All others get the 90s default — still correct to record them for completeness.
#
# NOTE: "verifier", "analyst", and "tracker" are NOT in chats.py _VALID_ROLES
# (which is frozenset{master, agent, observer, critic, architect, intake}).
# This script bypasses _VALID_ROLES validation by writing directly via _append.
# Safe for now: _threshold_for_session only checks _REPLY_OVERDUE_BY_ROLE, not
# _VALID_ROLES. If read-side validation is added later, these roles would need
# to be added to _VALID_ROLES first.
MEMBER_ROLES: dict[str, str] = {
    "d13300a7-da03-4ff3-9e47-a7ef463b09dc": "master",      # khimaira-0
    "e7c579fc-9e8d-4e7f-8ea4-3cab6e124ca1": "architect",   # architect-1
    "7188a905-71a0-4980-8db5-6a4d45558522": "critic",      # critic-1
    "c4d1b051-1a51-4d63-b2ff-749147d6ddfc": "verifier",    # verifier-1
    "32355798-b876-41c3-b0f8-0c7a52873b0b": "intake",      # intake-1
    "79466da7-9b08-4bab-b194-ad28f764c124": "analyst",     # analyst-1
    "c6bb382d-983e-488d-aad3-a067c10de65e": "agent",       # agent-1
    "2bb8bb23-ea53-4b51-8a19-6cfc06e4b95b": "agent",       # agent-2
    "23f23307-80ef-434a-a206-2c9a0bf84402": "agent",       # agent-3
    "700c1b54-b0ec-4fe3-911c-ad2afbf511d0": "observer",    # observer-1
    "4a29a280-27dd-4844-89b9-690421110236": "tracker",     # khimaira-6 (tracker-1)
}


def backfill(chat_id: str, member_roles: dict[str, str], *, dry_run: bool = False) -> None:
    room = load_room(chat_id)
    existing_meta = dict(room.get("meta") or {})

    if existing_meta.get("member_roles"):
        print(f"[{chat_id}] member_roles already set — skipping.")
        return

    new_meta = {
        **existing_meta,
        "kind": META,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "member_roles": member_roles,
    }

    if dry_run:
        print(f"[{chat_id}] DRY RUN — would append META with member_roles={member_roles}")
        return

    _append(chat_id, new_meta)
    print(f"[{chat_id}] Backfilled member_roles for {len(member_roles)} members.")

    # Verify
    room2 = load_room(chat_id)
    got = room2["meta"].get("member_roles")
    assert got == member_roles, f"Verification failed: {got!r} != {member_roles!r}"
    print(f"[{chat_id}] Verified — member_roles now: {got}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    backfill(CHAT_ID, MEMBER_ROLES, dry_run=dry_run)
