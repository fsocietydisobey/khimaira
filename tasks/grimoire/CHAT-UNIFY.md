# Grimoire — unify Q&A on ONE context-aware chat sidebar

## Goal

Retire the standalone notes ask-bar. There is ONE shared chat-sidebar component
across the whole grimoire; its scope **follows what you're viewing** (Joseph,
decision — context-aware):

- **Guide open** → chat scoped to that guide (unchanged; already shipped).
- **Note open** → chat scoped to THAT note (new — same UX as a guide).
- **Nothing open** (notes list) → notebook-wide ask across ALL notes, `@` to
  reference any note (today's ask-bar capability, moved into the sidebar).

One component, one place to maintain, consistent UX.

## Backend (void-null) — queued AFTER repo-scoping

### 1. Per-note chat — small (lift a restriction)

`POST /notes/{id}/chat` currently 404s on a non-guide note (guide-only). Lift that:
a note IS a record with `raw_text`; the chat loop already operates on
`llm_view(record)`, the sidecar store (`notebook/chats/<id>.json`),
`_invoke_agentic_grounded`, and the shared poll route — all reuse unchanged. Pass
the note's `repo` as the agentic `--add-dir` (same as guides). **Sensitive notes:
chat is already answer-only (edits disabled) + redacted — verify it holds for the
note path too.** A short/unstructured note body is fine as the grounding doc.

Verify: a note's chat answers grounded in its content + repo; a sensitive note's
chat is answer-only and never echoes a secret.

### 2. Notebook-wide chat — the bigger piece (nothing open)

The "nothing open" case = the existing notebook-wide ask (`answer_question` over
retrieval + `@` refs) surfaced as a conversational sidebar. **Impl decision for you
to propose:**
- **Full (preferred for consistency):** reuse the chat loop with all-notes
  retrieval as the grounding context + a notebook-level sidecar (keyed to
  project/notebook scope, e.g. `notebook/chats/_notebook_<project>.json`) so it's
  multi-turn + persistent like per-record chat. `@`-refs resolve to specific notes
  injected into context.
- **MVP (if full is too big):** keep `answer_question` one-shot, render it in the
  sidebar with client-accumulated history; flag that it's not true multi-turn.

Propose which, with the tradeoff — I'll gate. Contract: keep `POST /notes/{id}/chat`
for the record case; add a notebook-wide endpoint (e.g. `POST /notes/chat`
`{message, refs?}`) for nothing-open, both polling the shared job route with
`kind:"chat"`.

## Frontend (master) — after current queue; per-note wiring unblocks once §1 lands

- **Generalize `GuideChatPanel` → shared `ChatPanel`**, used in: the guide reader,
  the note reader, and the notes-list (nothing-open) context.
- **Retire the top ask-bar** + its suggestion chips (move the suggestion chips into
  the notebook-wide sidebar's empty state so they're not lost).
- **Context wiring:** a record is open (guide OR note) → `POST /notes/{id}/chat`;
  nothing open → the notebook-wide endpoint. `@`-reference autocomplete in
  notebook-wide mode (reuse whatever the old ask-bar used).
- **Persistence:** load per-record history on open (existing); load the
  notebook-wide history when nothing's open.
- Reuse everything already built (message bubbles, citations, cost, grounding badge,
  inline edit-diff, clear/compact) — this is a re-host of the same panel into more
  contexts, not a rewrite.

## Verify

- Open a note → sidebar chats about that note (grounded, persists across reopen).
- Nothing open → sidebar asks across all notes, `@`-ref works, (multi-turn if full).
- Guide chat unchanged. The old top ask-bar is gone. One component renders all three.
- Sensitive note chat: answer-only, no secret echo.
