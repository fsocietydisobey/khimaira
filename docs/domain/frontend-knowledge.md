# [DOMAIN] Domain Knowledge — [ROSTER]

> **Template — copy to `docs/domain/<domain>-knowledge.md` when creating a new lead role.**
> Lead sessions read this doc on bootstrap (full content) and append entries before session-end.
> See `docs/domain/README.md` for the three-axis substrate distinction.

_Last meaningful update: <ISO-8601 timestamp> by <session-slug>_

---

## Patterns

Conventions this codebase actually uses (not what docs say it should).
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <pattern statement>`.

<!-- Example entry (delete on first real use):
_2026-05-26 by backend-lead-1:_ All FastAPI routes that resolve a session name
must catch ValueError and re-raise as HTTPException(404) — see
packages/khimaira/CLAUDE.md "Every endpoint that resolves a session name needs
unknown-name coverage" for rationale.
-->

## Footguns

Things that have broken before, why, and what the fix was.
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <symptom> → <root cause> → <fix>`.

<!-- Example entry:
_2026-05-22 by backend-lead-1:_ SSE reconnect dropped messages on per-chat
basis → cursor was global across chats → daemon-side per-(session, chat)
cursor + cursor-advance-on-yield (fix in commit 2706722).
-->

## Key files

Files a new lead needs to know about + one-line why.
Add entries as: `- \`<path>\` — <one-line purpose>`. _(no timestamp needed — path is the key)_

## Open questions

Unresolved design decisions the next lead session should be aware of.
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <question> — <current best guess or N/A>`.

## Recent significant changes

Last 5-10 meaningful changes with rationale (not just a git log).
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <change summary> — <why>`.

---

## Write protocol (read before appending)

**Append-only with structured sections** — never modify existing entries. New
knowledge goes UNDER the relevant section header with author + timestamp.
Per architect-1 enumeration msg-883b3e43c9d1 (Phase 1A coverage).

**When to write:**
- After learning something non-obvious about the domain (pattern, footgun)
- Before session-end if you accumulated knowledge worth preserving
- When discovering a key file the next lead would benefit from knowing

**When NOT to write:**
- Trivial commits (git log captures those)
- Session-specific tactical decisions (those go in `session_log_decision`)
- Project-wide work-in-flight state (that's tracker's STATE.md)

**Read protocol:**
- Lead reads full doc on SessionStart (bootstrap)
- Lead can re-read mid-session as memory refresh; doc IS the canonical role memory
- Other roles (architect, critic, etc.) may read for cross-cutting context but
  don't write — only the named lead role updates its own doc

**Synthesis pass (Phase 2, deferred):** when this doc exceeds 500 lines, a
separate session synthesizes entries into refined consolidations and archives
originals to `.archive/`. NOT triggered automatically; explicit Phase 2 task.
