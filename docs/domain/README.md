# Domain Knowledge — Three-Axis Substrate

This directory holds domain-lead knowledge docs — one of three orthogonal
persistent-state substrates in khimaira.

Per architect-1 enumeration msg-883b3e43c9d1 (Phase 1A persistence consult).

## The three substrates

| Substrate | Scope | Audience | Cadence | Location |
|---|---|---|---|---|
| **STATE.md** (tracker) | Project work-in-flight | All sessions | Task state change | `STATE.md` |
| **session JSONL** | One session's decisions | Same session, future ref | Each decision | `~/.local/state/khimaira/sessions/<id>/*.jsonl` |
| **Domain knowledge** | Role's codebase understanding | Next instance of same role | Learning + session-end | `docs/domain/<domain>-knowledge.md` |

**They don't overlap.** Each answers a different question:
- STATE.md: "What's in flight across the project right now?"
- session JSONL: "What did I commit to during this session?"
- Domain knowledge: "What does my role need to know about this domain that isn't in the code?"

## Creating a new domain doc

When Phase 1 of the topology RFC adds a new lead role (e.g. `backend-lead`):

1. Copy `_template-knowledge.md` to `<domain>-knowledge.md`
2. Replace `[DOMAIN]` and `[ROSTER]` placeholders in the header
3. Add the new doc path to the lead role's `## Bootstrap behavior` section
4. Lint test `test_each_lead_role_has_knowledge_doc` will catch missing docs

## What goes in (vs what doesn't)

**Goes in:**
- Patterns this codebase actually uses (not generic best practice)
- Footguns: bugs found + root causes + fixes (so the next lead doesn't repeat)
- Key files: which files a new lead must understand + one-line each
- Open design questions: unresolved decisions the next lead should weigh in on
- Recent significant changes: last 5-10 meaningful changes + rationale

**Does NOT go in:**
- Session-tactical decisions (use `session_log_decision`)
- Project work-in-flight state (that's STATE.md)
- Generic best practice (that's `~/.claude/rules/`)
- Implementation details captured by git log

## Write protocol

**Append-only with structured sections.** Add entries with author + timestamp:
```
_<YYYY-MM-DD> by <session-slug>:_ <entry text>
```

Never modify existing entries. Structured append preserves history while
keeping the doc grouped by concern. Phase 2 synthesis pass (deferred) will
consolidate older entries when docs exceed ~500 lines.

## Phase 1B (deferred) — Séance indexing

Once domain docs exist, Séance auto-indexes them alongside source code. Leads
can query natural-language ("what's the pattern for X?") and retrieval handles
cross-doc lookup. Phase 1B is a one-line Séance config addition; no doc rework
needed.

## Phase 2+ (deferred)

- Synthesis pass when docs exceed 500 lines
- TTL archive for entries older than N months
- SessionEnd hook enforcement IF convention proves unreliable
- Codebase-reality reconciliation pass

See `docs/khimaira-roster-topology-rfc.md` for the topology context.
