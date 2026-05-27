# Backend Domain Knowledge — khimaira-dev

> **Lead sessions read this doc on bootstrap (full content) and append entries before session-end.**
> See `docs/domain/README.md` for the three-axis substrate distinction.

_Last meaningful update: 2026-05-26 by backend-lead-1_

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

_2026-05-26 by backend-lead-1:_ All FastAPI routes resolving a session name must
catch `ValueError` and re-raise as `HTTPException(404)` — unhandled ValueError
becomes HTTP 500 with a stack trace. Pattern: `except ValueError as e: raise
fastapi.HTTPException(404, str(e))` at the route boundary; services/repos throw,
routes catch and transform.

_2026-05-26 by backend-lead-1:_ `detect_project(cwd)` in `session_end_utils.py`
uses longest-prefix-wins via `path.is_relative_to(root_path)` — walks all entries
in `~/.local/share/khimaira/leads/`, picks the one whose root_path shares the
deepest prefix with the cwd. Basename fallback when no match. Variables
`best_name`/`best_depth` must be initialized BEFORE the try block — otherwise
`FileNotFoundError` (leads dir absent) leaves them unbound and raises
`UnboundLocalError`.

_2026-05-26 by backend-lead-1:_ `session_end.py` qualifies the mnemosyne domain
key as `<project>:<domain>` (e.g. `khimaira:backend`) before distilling. Falls
back to bare domain string on `detect_project` failure. This lets multiple projects
share one mnemosyne server without key collision.

_2026-05-26 by backend-lead-1:_ `_relative_or_absolute()` helper in `sync.py`
wraps `path.relative_to(root).as_posix()` in a try/except ValueError, falling back
to `path.as_posix()`. Needed when `knowledge_dir`, `roles_dir`, or `themis_dir` in
the manifest point to absolute paths outside the project root (e.g. cross-repo
output dirs). Without this, `sync_leads` crashes on `.relative_to()` for those
paths.

_2026-05-26 by backend-lead-1:_ `khimaira leads sync` uses `Manifest.prefix` to
namespace lead names and Themis rule IDs. Pattern: `_lead_name(domain, prefix)` →
`f"{prefix}-{domain}-lead"` if prefix else `f"{domain}-lead"`. Themis rule IDs
follow same shape: `IN-[PREFIX-]DOMAIN-LEAD-1/2` uppercased. The Option C contract
keeps the prefix ONLY on session-name artifacts (role doc filename, Themis IDs) —
mnemosyne keys and knowledge doc filenames stay bare-domain
(e.g. `khimaira:backend`, `backend-knowledge.md`).

## Footguns

Things that have broken before, why, and what the fix was.
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <symptom> → <root cause> → <fix>`.

<!-- Example entry:
_2026-05-22 by backend-lead-1:_ SSE reconnect dropped messages on per-chat
basis → cursor was global across chats → daemon-side per-(session, chat)
cursor + cursor-advance-on-yield (fix in commit 2706722).
-->

_2026-05-26 by backend-lead-1:_ `UnboundLocalError` in `detect_project` →
`best_name`/`best_depth` declared inside try block → `FileNotFoundError` (leads dir
absent) exits the try before assignment → names unbound at return. Fix: hoist both
to `best_name = ""; best_depth = -1` before the try block. Caught by
`test_detect_project_falls_back_when_no_leads_dir`.

_2026-05-26 by backend-lead-1:_ Dead code cluster in `sync.py` (4 deleted symbols):
`_TEMPLATE_KNOWLEDGE` constant (8 `.parent` traversals pointed outside repo root,
never referenced), `_RULE_1_SUFFIX`/`_RULE_2_SUFFIX` (defined, never used),
`tempfile.TemporaryDirectory()` + `tmp_root` in `check_drift` (temp dir created but
never written to — `check_drift` does in-memory comparison). All confirmed dead by
grep; deleted without replacement.

_2026-05-26 by backend-lead-1:_ Mnemosyne server (port 8766) exposes ONLY:
`GET /health`, `GET /domains`, `POST /query`, `POST /distill`. No eviction/delete
API. Synthetic test pairs written via distill persist until server restart — low
harm for single low-signal entries, but worth knowing if you're smoke-testing
distill and want a clean slate: restart mnemosyne, not the khimaira daemon.

## Key files

Files a new lead needs to know about + one-line why.
Add entries as: `- \`<path>\` — <one-line purpose>`. _(no timestamp needed — path is the key)_

- `packages/khimaira/src/khimaira/leads/manifest.py` — `Manifest` dataclass + `load_manifest(project_name)`, central path `~/.local/share/khimaira/leads/<name>.toml`, `root_path` + `prefix` fields
- `packages/khimaira/src/khimaira/leads/sync.py` — `sync_leads(project_name)`, `check_drift(project_name)`, `_lead_name(domain, prefix)`, `_themis_rule_ids(domain, prefix)`, `_relative_or_absolute()` helper
- `packages/khimaira/src/khimaira/hooks/session_end_utils.py` — `detect_project(cwd)` longest-prefix-wins; `_central_leads_dir()` path helper
- `packages/khimaira/src/khimaira/hooks/session_end.py` — distills domain knowledge on session exit; qualifies key as `<project>:<domain>`
- `packages/khimaira/src/khimaira/hooks/session_start.py` — `_inject_domain_memory()` — fetches session name, detects lead role, queries mnemosyne, injects PROVISIONAL block; fail-open throughout
- `packages/khimaira/src/khimaira/leads/mnemosyne_client.py` — `distill(domain, transcript, session_slug)` and `query(domain)` — thin HTTP clients to mnemosyne at port 8766; both fail-open

## Open questions

Unresolved design decisions the next lead session should be aware of.
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <question> — <current best guess or N/A>`.

_2026-05-26 by backend-lead-1:_ Mnemosyne has no eviction API — should there be one? — Low priority while entries are low-signal/small volume; server restart is the escape hatch. Worth adding `DELETE /domains/{key}` if smoke-testing becomes routine.

_2026-05-26 by backend-lead-1:_ `_inject_domain_memory` in session_start queries mnemosyne with the full `<project>:<domain>` key. If mnemosyne is down (URLError), the block is silently skipped. Should the PROVISIONAL block be injected even on failure (e.g. empty/stub)? — Current answer: fail-open (no injection) is safer than injecting stale data.

## Recent significant changes

Last 5-10 meaningful changes with rationale (not just a git log).
Add entries as: `_<YYYY-MM-DD> by <session-slug>:_ <change summary> — <why>`.

_2026-05-26 by backend-lead-1:_ `leads/sync.py` — added `Manifest.prefix` field +
`_lead_name(domain, prefix)` + `_themis_rule_ids(domain, prefix)` + `_relative_or_absolute()`.
Why: jeevy project uses `jp-` prefix for all lead names (jp-frontend-lead, jp-backend-lead,
jp-data-lead); bare `{domain}-lead` naming broke the naming contract. Option C keeps prefix
on session-name artifacts only.

_2026-05-26 by backend-lead-1:_ `hooks/session_end_utils.py` — hoisted `best_name`/`best_depth`
before try block in `detect_project`. Why: `FileNotFoundError` (leads dir absent) exited the
try before assignment, causing `UnboundLocalError` at return. Caught by lint test
`test_detect_project_falls_back_when_no_leads_dir`.

_2026-05-26 by backend-lead-1:_ `leads/sync.py` — deleted 4 dead symbols: `_TEMPLATE_KNOWLEDGE`,
`_RULE_1_SUFFIX`, `_RULE_2_SUFFIX`, `tempfile.TemporaryDirectory()`+`tmp_root` in `check_drift`.
Why: confirmed dead by grep; `_TEMPLATE_KNOWLEDGE` path resolved outside repo root (8 `.parent`
traversals), never referenced; suffix constants defined but never used; temp dir created but
`check_drift` does in-memory comparison.

_2026-05-26 by backend-lead-1:_ `hooks/session_start.py` — added `_inject_domain_memory()`
that fetches the session name from daemon, detects if it's a lead session, queries mnemosyne
with `<project>:<domain>` key, and injects a PROVISIONAL block into the SessionStart context.
Why: leads need their accumulated domain memory surfaced on bootstrap without requiring explicit
reads.

_2026-05-26 by backend-lead-1:_ jeevy manifest + role/Themis generation — created
`~/.local/share/khimaira/leads/jeevy.toml` with `name=jeevy, prefix=jp, root_path=/home/_3ntropy/work/jeevy_portal`;
`khimaira leads sync jeevy` generated jp-{frontend,backend,data}-lead.md roles +
themis YAMLs + seeded knowledge docs. Why: extending leads automation to the jeevy
project, demonstrating prefix-namespacing works across projects.

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
