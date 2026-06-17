# Wake by session-identity, not window-title (finish task #16 for the wake path)

> Status: SPEC / ready-to-build · audit-grade root-cause done 2026-06-17 · scoped by khimaira-0
> Corpus: `shared-docs/ESCAPED-BUGS-LOG.md` → `wake-targets-window-by-title-not-session-identity`

## The bug (audit-grade)

The daemon's roster wake is **100% kitty-window-title discovery**, with no identity
fallback. A legitimately-roled chat-member agent whose **window title isn't
role-shaped** is silently unwakeable.

Concrete: `void` joined the roster chat with `role=agent`, but its kitty window was
titled `void`. The idle-but-owing watchdog never reached it; master had to hand-nudge
via `kitty @ --match title:^void$`. Routing muther's *next* fix through void was blocked
*by* void being unwakeable — the gap blocked fixing the gap.

### Why (three compounding facts, all verified in code)
1. **`_discover_roster_windows` (roster_recovery.py:206) drops non-role-titled windows.**
   Line 290–291: `if not role: continue`, where `role = infer_role_from_name(title)`.
   `infer_role_from_name("void")` → None → dropped.
2. **`window_id` registration exists but is gated.** session_start.py:1046–1068 POSTs
   `{slot, window_id}` to `/api/sessions/{id}/slot` **only when `KHIMAIRA_ROSTER_SLOT`
   is set** (roster-launched sessions). Standalone sessions skip it entirely.
3. **Nothing persists or consumes `session_id→window_id`.** `set_session_slot`
   (sessions.py:916) stores `roster_slot` only — not the window_id. And `_process_window`
   (roster_recovery.py:1478) consumes ONLY title-discovered windows. There is no
   identity→window lookup anywhere in the wake path.

So **task #16 ("move liveness off title-match onto session-id") was completed for the
liveness *read* but never for the *wake* path.** This finishes it.

## The fix — 3 components (additive; the title path stays, so the LIVE watchdog is untouched)

### 1. Persist `session_id → window_id`
- In `sessions.py`: store a `window_id` field on the session record (status.json), with a
  setter (`set_session_window(session_id, window_id)`) and a getter
  (`get_session_window(session_id) -> int | None`).
- Expose via the API: `POST /api/sessions/{id}/window {window_id}` (or extend the slot
  endpoint to also persist window_id when present — but a dedicated endpoint is cleaner +
  decoupled from the slot's TRAP-2 token verification).

### 2. Register it unconditionally
- session_start.py: register `window_id` whenever `$KITTY_WINDOW_ID` is present — NOT
  gated on `KHIMAIRA_ROSTER_SLOT`. Keep the existing slot POST for slotted sessions; ADD
  an always-on window registration for every hooked session. Best-effort, never blocks
  boot (same discipline as the existing POSTs).
- NOTE the standalone-hook caveat: a truly standalone session that doesn't run the
  khimaira hooks at all (themis-hook-dormant-standalone, same corpus) won't register
  either — that's a *separate* hook-wiring gap. This fix covers every session that runs
  session_start.py (all roster sessions + any with the global hook).

### 3. Union registered windows into the wake
- In the watch loop (roster_recovery.py ~1776, `for win in windows`), build the window
  set as **title-discovered ∪ registered**: for each roster member session_id that has a
  registered window_id but was NOT found by title-discovery, synthesize a `win` dict
  (`{window_id, role (from chat member_roles), raw_name (session name/id), ...}`) and
  include it. Dedup by window_id.
- This is ADDITIVE — title-discovery is unchanged, so every currently-wakeable window
  keeps working exactly as now. Only previously-invisible registered windows get added.

## Test contract (the catching-test for the corpus seam)
- `test_wake_by_registered_window_id`: register `session_id→window_id` for a session with
  a NON-role-shaped title (e.g. `void`), give it an owing obligation + idle >5min →
  assert `_process_window`'s loop includes it and the wake targets its registered
  window_id. (Currently: dropped, never woken.)
- `test_title_discovery_still_primary`: a normally role-titled window with NO registration
  is still discovered + wakeable (no regression to the live path).
- `test_dedup_title_and_registered`: a window that is BOTH title-discovered AND registered
  appears once (no double-wake).
- Live-verify post-deploy (the discipline this whole arc taught): re-spawn `void` as a
  properly-titled `agent-1` OR keep its `void` title but confirm registration → wake fires
  on it by identity; a quiet registered seat stays silent.

## Deploy
Daemon bounce (muther notified first), same as the watchdog. The change touches the wake
path the live watchdog uses → run the full roster_recovery + watchdog test suite + the
new tests green before bouncing. guard5 stays OFF; Path 3 parked.

## Collaboration note
If routing to a helper agent (void/fresh): spawn it with a **roster-role-shaped window
title** (`agent-1`) so it's wakeable *during* the build — otherwise the same gap blocks
the collaboration (as it did this session). Bootstrap it as a real roster member, not an
ad-hoc chat invite.
