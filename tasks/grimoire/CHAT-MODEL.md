# Grimoire Phase 3 (REVISED) — Per-Guide Conversational Chat

## Goal

Replace the two-button ANSWER/REVISE research toolbar with a **per-guide,
persistent conversational chat — like Claude Code scoped to a study guide.**
You talk to it; it researches (codebase + web) and answers by default, and
**when you tell it to change something, it edits the guide (auto-apply)**.
Multi-turn, remembers the conversation, with clear + compact controls.

## The locked design (Joseph, decision e2fba504)

- **One chat per guide**, **persistent** — reopen a guide, the conversation's
  still there.
- **Researches + answers by default** (agentic, code + web, grounded).
- **Edits on instruction, AUTO-APPLIED** — the agent decides answer-vs-edit per
  message; an edit applies immediately (diff shown inline), undo via version
  history (already shipped). No confirm click, no separate mode.
- **Multi-turn** with **clear** (reset) + **compact** (summarize-to-preserve).
- Same shape as this Claude Code session.

## What it reuses (nothing is rebuilt)

- **Async job+poll** (`schedule_research_*`, `GET /notes/research/{job_id}`, the
  in-memory job store) — every chat turn is a slow agentic call; this is its engine.
- **`_invoke_claude_agentic`** — the tool-granted research invocation + the
  transcript-verified grounding + per-call config dir.
- **`splice_section`** + `update_note(raw_text=)` — the edit-apply path (auto-apply
  writes through this; version-history snapshot + reprocess + organize-on-edit all
  fire, unchanged).
- **The Phase-4 version-history snapshot** — what makes auto-apply safe (undo).
- The REVISE diff view + citation chips + grounding-unverified badge + cost footer
  from the current frontend — they feed the chat message rendering.

## Backend (void-null)

1. **Per-guide chat store.** Persist conversation per guide. A `chat_history`
   field on the note record (list of `{role, content, edit?: {section_anchor?,
   diff, applied_at}, cost?, grounding?, ts}`) OR a sidecar `notebook/chats/
   <note_id>.json` (fold/atomic like the note store). Loaded on open, appended each
   turn. Excluded from the note's `raw_text`/pipeline entirely.

2. **`POST /notes/{id}/chat` `{message, max_budget_usd?}`** → async job (reuse the
   job infra) → `{job_id, status:pending}`. The job:
   - loads the guide's chat history (compacted form) + the guide `raw_text` + repo,
   - runs `_invoke_claude_agentic` with: the conversation history + the new message
     + research tools (Read/Grep/Glob/WebSearch/WebFetch, `--add-dir repo`) + a
     system prompt instructing it to **research + answer, and when the user asks for
     a change, produce the edited section/guide**,
   - **structured output** distinguishes `{answer}` vs `{answer, edit:{section_anchor?,
     new_text}}` — the agent's own answer-vs-edit routing (NOT a separate endpoint),
   - if `edit`: **auto-apply** via `splice_section`→`update_note(raw_text=)` (→
     version-history + reprocess + organize-on-edit, all existing),
   - append the user msg + assistant response (+ edit diff) to the chat history.

3. **Poll** reuses `GET /notes/research/{job_id}` → add `kind:"chat"` →
   `{status, message:{content, edit?:{diff, applied}}, grounding, total_cost_usd}`.

4. **`POST /notes/{id}/chat/clear`** → wipe the guide's chat history.

5. **`POST /notes/{id}/chat/compact`** → one `_invoke_claude` (tool-less) call:
   summarize the conversation so far → replace the older messages with a single
   summary message; keep the tail. Frees context so long chats stay affordable
   (each turn passes history into the agentic call — unbounded history = unbounded
   cost). This is load-bearing, not cosmetic.

6. **Debounce the re-organize** on rapid chat edits — a chatty edit sequence
   shouldn't fire a full `organize_library` per edit. Coalesce (e.g. a short
   trailing-debounce, or skip organize on chat-edits and let the sweep handle it).

## Frontend (khimaira-master)

- **Chat panel in the guide reader**, replacing the ANSWER bar + REVISE buttons.
  Message list (user + assistant bubbles; assistant messages render markdown +
  citation chips + cost + grounding-unverified badge; **edit messages show the diff
  inline** with an "applied" marker) + an input box.
- **Send** → `POST /notes/{id}/chat` → poll the job → append the response (+ edit
  diff) to the list; the guide body live-updates when an edit applies.
- **Clear** + **Compact** buttons (like this session's).
- **Persistent** — load the guide's `chat_history` on open; the conversation
  survives reopen.
- Reuse the diff view + citation/cost/grounding components already built.

## Verify

- Multi-turn: ask a question → get a grounded answer; then "update the X section to
  add Y" → the guide's `raw_text` changes, the diff shows in the chat, version
  history has the prior version; reopen the guide → conversation persists.
- Clear wipes; compact summarizes + shrinks the history passed to the next call.
- No exit-1/143 (the reliability batch is deployed); rapid edits don't fire N
  organizes. `/api/version` stale=false. Specter DOM-verify via evaluate_js.
