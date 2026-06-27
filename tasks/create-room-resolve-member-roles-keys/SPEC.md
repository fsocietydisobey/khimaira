# SPEC — resolve `member_roles` keys name→UUID in `create_room` + `POST /chats`

> Filed 2026-06-19 from a CONFIRMED live escape (SLM-build onboarding). See
> `shared-docs/ESCAPED-BUGS-LOG.md` → `create-room-stores-member-roles-keys-unresolved`.

## Problem

`create_room` (`packages/khimaira/src/khimaira/monitor/chats.py:866-874`) and the
`POST /chats` endpoint (`packages/khimaira/src/khimaira/monitor/api/chats.py:1619-1625`)
persist the caller-passed `member_roles` dict **verbatim**. They resolve
`member_session_ids` name→UUID via `_resolve_or_uuid` (chats.py:829) but never resolve
the **keys** of `member_roles`. So a name-keyed call —
`create_room(members=["livyatan"], member_roles={"livyatan": "agent"})` — stores
`member_roles={"livyatan": "agent", "<creator-uuid>": "master"}`: the member's role under
a **name** key while the `members` dict is correctly UUID-keyed.

Themis `_resolve_role_from_jsonl` (`api/themis.py:176`) looks up `member_roles.get(sid)`
by session **UUID** → miss → created_by miss → name-inference miss → Layer-4 fail-closed
`_UNRESOLVABLE` → `IN-UNRESOLVABLE` blocks every write/commit for that member until an
explicit `chat_grant_role` (which writes a UUID-keyed entry).

**Confirmed live**, not latent: the bootstrap-roster skill passes UUID keys (safe), but
direct ad-hoc `create_room` usage is unguarded and bit the SLM-build onboarding.

## Fix

1. In `chats.create_room`: before storing, rewrite `member_roles` keys through
   `_resolve_or_uuid` — symmetric with how `member_session_ids` are resolved at line 829.
   Keep the existing `setdefault(creator_session_id, ROLE_MASTER)` (already UUID-keyed).
   - Decide policy for a key that doesn't resolve: raise `ValueError` (fail-loud, matches
     the phantom-member guard `_assert_session_registered`) rather than silently dropping —
     a silently-dropped role re-creates the exact fail-closed lockout this fixes.
2. Apply the same resolution at the `POST /chats` boundary OR rely on the chats-layer fix
   (preferred: fix once in `create_room` so every caller — endpoint, tests, direct — is
   covered; the endpoint just forwards `req.member_roles`).
3. Consider the same audit for any other path that writes `member_roles` from caller input
   (grep `member_roles\[` / `meta\["member_roles"\]` writers) — `invite` already resolves
   `invitee_session_id`→UUID (chats.py:929) so it is safe; verify `chat_grant_role` and the
   reseat/transfer paths key by UUID (they appear to via resolved session ids).

## Catching-test (REQUIRED — the seam this closes)

`packages/khimaira/tests/` — L1 real-producer→consumer:

```python
def test_create_room_resolves_name_keyed_member_roles(isolated_chats):
    # register sessions with names, then create_room with NAME-keyed member_roles
    room = create_room("masterX", ["livyatan"], member_roles={"livyatan": "agent"})
    roles = room["meta"]["member_roles"]
    # (a) NO name keys survive — every key is a resolvable UUID
    assert all(_looks_like_uuid(k) for k in roles)
    # (b) the role resolves by UUID with NO chat_grant_role
    livyatan_uuid = _resolve_or_uuid("livyatan")
    assert resolve_session_role(livyatan_uuid) == "agent"
```

Plus the fail-loud case: `create_room(..., member_roles={"ghost-name": "agent"})` where
`ghost-name` isn't registered → `ValueError` (not a silent drop).

## Secondary (same incident, optional follow-up)

Ergonomic gap: a role-unbound member hard-blocks ALL writes only **after** accept, with no
onboarding-time signal. Consider `invite()`/`create_room` warning (or granting role
pre-accept) when a non-role-named roster member ends up role-unbound, so the failure is
visible at membership time instead of on the member's first tool call.

## Out of scope

The Themis Layer-4 fail-closed behavior itself is correct (#61) — do NOT weaken it. The fix
is producer-side key resolution, not consumer-side leniency.
