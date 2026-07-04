# Grimoire — Repo-scoped personal/behavior context

## Goal

Today `_personal_context()` concatenates EVERY personal-tab note into EVERY LLM
call (structuring, ask-synthesis, per-guide chat). That's correct for global VOICE
rules but wrong for DOMAIN rules — a jeevy-fabrication-domain rule shouldn't ride a
khimaira guide's structuring call. Make the personal context **repo-scoped**:
global notes always inject; domain notes inject only when the target's repo matches.

## The minimal design — reuse the existing `repo` field (no new field)

A personal note already carries a `repo` field (the seed note uses
`notes.GENERAL_REPO`). Key scoping on it:

- **Global note** — `repo == GENERAL_REPO` (the sentinel meaning "all"). Always
  injected. The seed "Voice & structure rules" note + the new engineering-principles
  note are global.
- **Domain note** — `repo == "<a real repo tag>"` (e.g. `jeevy_portal`). Injected
  ONLY when the note/guide being processed has that same `repo`. The jeevy
  fabrication-domain note carries `repo="jeevy_portal"`.

## Change

`_personal_context(target_repo: str | None = None) -> str` (notebook_pipeline.py:331):
- Fetch personal-tab notes as today.
- Include a note if `note.repo == GENERAL_REPO` **OR** (`target_repo` is not None AND
  `note.repo == target_repo`). Order: global notes first, then the matching domain
  note(s), so global voice leads.
- Keep the 6000-char cap AFTER filtering (the cap now applies to the smaller,
  relevant set — domain calls get global+their-domain, not everyone's domain).
- `_prepend_personal_context(instruction, target_repo)` threads `target_repo`
  through.

**Injection sites must pass the target repo** (each already knows it — it's the
record's `repo`):
1. Structuring pipeline (`transform_note`/`_run_once` for a note/guide) → pass that
   record's `repo`.
2. `revalidate_note` currency pass → the record's `repo`.
3. Ask-synthesis (`answer_question`) → the asking context's repo if it has one, else
   None (global-only).
4. Per-guide chat (`notebook_chat` `build`) → the guide's `repo`.

Enumerate every current caller of `_personal_context()` / `_prepend_personal_context()`
and thread the target repo through — a missed call site silently reverts to
global-only for that path (safe-degraded, but flag it).

## Back-compat

- `target_repo=None` → global notes only (current behavior for any path without a
  repo). Purely additive; existing global seed note keeps riding everything.
- No migration: the seed note already has `GENERAL_REPO`; new domain notes are
  created with their real repo tag.

## Verify

- A global note injects on a call with any `target_repo` and on `target_repo=None`.
- A `repo="jeevy_portal"` domain note injects when structuring/answering/chatting a
  `repo="jeevy_portal"` guide, and does NOT inject for a `repo="khimaira"` (or
  GENERAL_REPO) guide.
- The 6000-char cap still holds on the filtered set.
- Every injection site passes the correct repo (test each: structuring, revalidate,
  ask, chat).

## Sequencing

Small; queue to void-null AFTER sensitive-notes + priority-flags (this touches
`notebook_pipeline.py` which the sensitive-notes `llm_view` routing also touches —
land sensitive first to avoid a merge on `_personal_context`, which sensitive-notes
already modifies for the cross-note-leak fix). Note the overlap: sensitive-notes
routes `_personal_context` through `llm_view`; this change adds repo-filtering to the
same function — do them in order, not in parallel.
