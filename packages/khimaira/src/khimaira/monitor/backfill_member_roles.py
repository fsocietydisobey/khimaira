"""Backfill member_roles for existing chats.

Targets two gap classes (post-572ed46 analysis):
  STATE-A3: chats with NO member_roles at all — fail-open enforcement gap.
  STATE-B4: chats with PARTIAL member_roles — unlisted non-role-named
            members hit _UNRESOLVABLE → ALL tools hard-blocked.

Role assignment ladder (same as the resolver's own order):
  1. created_by → "master"
  2. infer_role_from_name(session_name) → inferred role
  3. else → "member" (neutral, empty ruleset — see member.yaml)

ORDERING REQUIREMENT:
  Part 1 (member.yaml + ROLE_MEMBER + daemon restart) MUST precede this
  script. VALID_ROLES is glob-derived at daemon import time; writing
  "member" entries before member.yaml exists + daemon reload manufactures
  the same B4 lockout this script is closing.

Usage:
  uv run python -m khimaira.monitor.backfill_member_roles --dry-run
  uv run python -m khimaira.monitor.backfill_member_roles
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from khimaira.monitor import chats as chats_mod
from khimaira.monitor import sessions as sessions_mod

_ACCEPTED = "accepted"


def _chat_ids() -> list[str]:
    """Return chat IDs for all non-archived chats."""
    d = chats_mod._chat_dir()
    if not d.exists():
        return []
    return [p.stem for p in d.glob("chat-*.jsonl")]


def _backfill_room(
    chat_id: str,
    *,
    dry_run: bool,
    verbose: bool = False,
) -> dict[str, Any]:
    """Analyse one chat and optionally backfill missing member_roles entries.

    Returns a summary dict:
      {chat_id, state, members_added: [{sid, session_name, role}],
       already_complete: bool, skipped_reason: str | None}
    """
    try:
        room = chats_mod.load_room(chat_id)
    except ValueError as exc:
        return {"chat_id": chat_id, "skipped_reason": str(exc), "members_added": []}

    meta: dict[str, Any] = room["meta"]
    members: dict[str, dict[str, Any]] = room["members"]
    existing_roles: dict[str, str] | None = meta.get("member_roles")
    created_by: str = meta.get("created_by", "")

    accepted_sids = [
        sid for sid, m in members.items() if m.get("state") == _ACCEPTED
    ]

    # Determine which accepted members are missing role assignments.
    missing: list[dict[str, str]] = []
    for sid in accepted_sids:
        if existing_roles is not None and sid in existing_roles:
            continue  # already assigned
        # Infer role using the same ladder as the resolver.
        if sid == created_by:
            role = chats_mod.ROLE_MASTER
        else:
            session_name = members[sid].get("session_name") or ""
            inferred = chats_mod.infer_role_from_name(session_name) if session_name else None
            role = inferred if inferred is not None else chats_mod.ROLE_MEMBER
        missing.append({"sid": sid, "session_name": members[sid].get("session_name", ""), "role": role})

    # Classify state.
    if existing_roles is None:
        state = "A" if missing else "A-empty"
    else:
        state = "B" if missing else "C"

    if not missing:
        return {
            "chat_id": chat_id,
            "state": state,
            "already_complete": True,
            "members_added": [],
            "skipped_reason": None,
        }

    if not dry_run:
        # Build updated member_roles: copy existing (if any) + add missing.
        new_roles: dict[str, str] = dict(existing_roles) if existing_roles is not None else {}
        for entry in missing:
            new_roles[entry["sid"]] = entry["role"]

        # Append a fresh META record with the updated member_roles.
        # append-only: load_room takes the LAST meta record as authoritative.
        updated_meta = dict(meta)
        updated_meta["event_id"] = chats_mod._new_event_id()
        updated_meta["ts"] = chats_mod._now_iso()
        updated_meta["member_roles"] = new_roles
        chats_mod._append(chat_id, updated_meta)

    return {
        "chat_id": chat_id,
        "state": state,
        "already_complete": False,
        "members_added": missing,
        "skipped_reason": None,
    }


def run(*, dry_run: bool, verbose: bool = False) -> int:
    """Run the backfill over all chats. Returns 0 on success."""
    chat_ids = _chat_ids()
    if not chat_ids:
        print("No chats found — nothing to backfill.")
        return 0

    total_added = 0
    skipped = 0
    already_complete = 0

    for chat_id in sorted(chat_ids):
        result = _backfill_room(chat_id, dry_run=dry_run, verbose=verbose)

        if result.get("skipped_reason"):
            skipped += 1
            if verbose:
                print(f"  SKIP  {chat_id}: {result['skipped_reason']}")
            continue

        members_added = result["members_added"]
        state = result.get("state", "?")

        if result.get("already_complete"):
            already_complete += 1
            if verbose:
                print(f"  OK    {chat_id} (state {state}) — all members have roles")
            continue

        action = "would add" if dry_run else "added"
        print(f"  {'DRY ' if dry_run else ''}state={state}  {chat_id}")
        for entry in members_added:
            name = entry["session_name"] or entry["sid"][:8]
            print(f"         {action} {name!r} → role={entry['role']!r}")
        total_added += len(members_added)

    mode = "DRY RUN" if dry_run else "APPLIED"
    print(
        f"\n[{mode}] {len(chat_ids)} chat(s) scanned: "
        f"{total_added} member(s) {'would be ' if dry_run else ''}assigned, "
        f"{already_complete} already complete, {skipped} skipped."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="khimaira monitor backfill-roles",
        description="Backfill missing member_roles entries in existing chats.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show already-complete chats too.",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
