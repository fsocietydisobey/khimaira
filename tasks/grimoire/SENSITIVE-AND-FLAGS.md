# Grimoire — Sensitive (credential-safe) notes + Priority flags

Two independent features. **Sensitive notes** is a security boundary — treat its
egress enumeration as load-bearing. **Priority flags** is a small deterministic
field mirroring `status`.

---

## Feature A — Sensitive / credential-safe notes

### Goal

A place to paste notes that contain credentials (API keys, connection strings,
tokens). The notebook still does everything it normally does — summarize,
organize, tag, make it searchable — **but no LLM, embedding, training export, or
log ever sees the actual secret values.** Joseph can still read/copy the real
creds in the reader.

### The invariant (the bug CLASS to close)

> A secret value from a sensitive note MUST NOT reach an LLM prompt, an embedding
> vector, a stored retrieval passage, a cross-note context blob, a training/SFT
> export, or a log line.

The fix is **structural, not prompt-enforced**: redact ONCE at write time, store a
redacted twin, and route every egress through the twin. The model can't leak what
it never receives.

### Data model (`notes.py` record)

```
+ sensitive: bool                 # default False
+ llm_text: str | None            # redacted twin of raw_text; only present when sensitive
+ redactions: list[{placeholder, kind}] | None   # what was masked (for the UI "what got hidden" panel); NEVER stores the secret value
  raw_text: str                   # UNCHANGED — the REAL text incl. secrets, human-only
```

- On create (and every `raw_text` update) of a sensitive note: run
  `redact_secrets(raw_text) -> (llm_text, redactions)` and store all three.
  `raw_text` keeps the real creds; `llm_text` has each secret replaced by a stable
  placeholder like `‹SECRET:api_key#1›`.
- `redactions` records `{placeholder, kind}` ONLY — never the masked value.

### The choke point (one accessor, used at EVERY egress site)

Add `notes.llm_view(record) -> str` returning
`record["llm_text"] if record.get("sensitive") else record["raw_text"]`.
**Replace every raw_text read below with `llm_view(record)`.** This is what makes
the class closed structurally — one accessor, audited call sites.

### Enumerated egress paths (audit each — BROKEN = must route through `llm_view`)

1. **Structuring pipeline** `transform_note`/`_run_once` (notebook_pipeline.py) —
   summary/technical/plain. **BROKEN** → feed `llm_view`. (Because the abstract/
   summary are then computed from redacted text, path 2 is automatically safe.)
2. **Organizer** `organize_library` (notebook_organizer.py) — reads title +
   abstract + tags (derived). **SAFE IFF** upstream (path 1) redacted, so the
   abstract never contains a secret. Verify it never reaches for `raw_text`.
3. **Embedding / retrieval** `_passage_text` (notebook_retrieval.py:108/113) —
   embeds raw_text / abstract+raw_text[:N]. **BROKEN** → `llm_view` (embed the
   redacted twin; the stored passage the ask-path re-surfaces is then clean too).
4. **Chat / research agentic** `build(guide=raw_text)` (notebook_chat.py:252) —
   **BROKEN** → `llm_view`. Also: **auto-apply edits DISABLED for sensitive notes**
   — the agent only ever sees redacted text, so any edit it splices back would
   overwrite real creds with placeholders. Sensitive chat = answer-only; short-
   circuit `_try_apply_edit` with a clear "edits disabled on sensitive notes" msg.
5. **`_personal_context`** (notebook_pipeline.py:340) — concatenates EVERY personal
   note's `raw_text` into EVERY LLM call. **CRITICAL cross-note leak** if a
   sensitive note lands in the Personal folder → route through `llm_view` per note
   (or exclude sensitive notes from the concat entirely).
6. **Training / SFT export** (`training` field, distiller, `notebook_create_study_guide`
   pair emission) — a sensitive note's content becoming oracle training data bakes
   the secret into the model. **BROKEN** → sensitive notes are HARD-excluded from
   any training/export path (`training=False` forced + skip in the distiller
   selector). Audit every producer of training pairs.
7. **Logs** — the "added <id>" line already logs id only (good). Ensure the
   structuring subprocess prompt (which contains the note body) is never logged for
   a sensitive note, and no error path dumps `raw_text`. Grep for `raw_text` in any
   `log.`/`logger.`/`print` and confirm none fire on the sensitive path.

### Detection — DETERMINISTIC, never an LLM

The whole point is the model never sees the secret, so LLM detection is
self-defeating; and a security boundary must not be probabilistic. Regex + entropy,
reusing the security-rule pattern set:

- Prefixed tokens: `sk-ant-`, `sk-`, `AKIA`/`ASIA`, `AIza`, `gh[posru]_`,
  `xox[baprs]-`, `glpat-`, bearer tokens.
- Assignments: `(API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY)\s*[:=]\s*<value>`
  → mask the value.
- PEM blocks: `-----BEGIN … PRIVATE KEY-----` … `-----END … -----` (whole block).
- Connection strings with inline creds: `://<user>:<PASS>@host`.
- JWTs: `eyJ[…].eyJ[…].[…]`.
- High-entropy catch-all: token len ≥ 20, base64/hex charset, Shannon entropy
  above threshold.

**Fail-safe DIRECTION (load-bearing):** on uncertainty, OVER-redact. Masking a
non-secret is recoverable (Joseph still has `raw_text`); under-masking leaks
irreversibly to the LLM. The default points at the recoverable failure.

Put detection in a new `notebook_redaction.py` with unit tests covering each
pattern + a "no false-negative on a realistic .env paste" test.

### Storage-at-rest — Joseph's one decision (defaulting to A, ship-now)

- **Model A (default, shipping):** persist real `raw_text` in the JSONL (plaintext,
  local/LAN-only store — same trust surface as a scratch cred file). Redaction
  governs LLM egress only.
- **Model B (hardening follow-up):** encrypt secret values at rest (age / a key in
  the machine `.env`), decrypt only for the UI reader.

Ship A; flag B as a fast-follow. (Redaction closes the irreversible LLM-leak; at-
rest encryption is the lower, separately-hardenable risk.)

### API / frontend

- `CreateNoteReq` + `UpdateNoteReq` accept `sensitive: bool`. Create/update run
  redaction when true.
- `GET /notes/{id}` returns `raw_text` (real, for the reader) + `redactions` (the
  hidden-items summary). List/search never return `raw_text` of a sensitive note in
  bulk — only via the single-note reader fetch. (Confirm list payloads omit or
  redact it.)
- **Frontend:** a "🔒 Sensitive" toggle on the capture box (or a dedicated Vault
  collection). Reader shows the real creds (readable/copyable) with a badge:
  "🔒 Sensitive — the assistant sees a redacted copy" + an optional "what's hidden"
  panel listing `redactions`. Chat panel on a sensitive note shows edits are
  disabled.

### Verify

- Paste a realistic `.env` (multiple key shapes) as a sensitive note → structuring
  completes, but summary/technical/plain + abstract + tags contain ZERO secret
  values (assert each placeholder-kind's real value is absent from every derived
  field). Embedding passage is redacted. Chat answer never echoes a key. A sensitive
  note in Personal folder does not leak into another note's structuring. Training
  export excludes it. Reader still shows the real creds. Chat edit is refused.

---

## Feature B — Priority flags (notes + study guides)

### Goal

A user-set `priority` dimension **alongside** `status` (independent axes: status =
lifecycle draft/processed/promoted/failed; priority = importance). On BOTH notes
and study guides.

### Backend (`notes.py`)

- `_VALID_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})`, default
  `"normal"` — mirror the `_VALID_STATUSES` pattern exactly.
- Add `priority` to: the record on both create paths (note + guide), `_index_stub`,
  `_NOTE_MUTABLE_FIELDS`, and `update_note` validation (invalid → ValueError → 422,
  same shape as status).
- **The LLM organizer must NOT touch `priority`** (it's user-owned) — confirm it's
  outside the organizer's mutation set (organizer only sets `tab_id`/label).
- API: PATCH accepts `priority`; `list_notes` supports `?priority=<v>` filter and
  priority-aware sort (`?sort=-priority` with urgent>high>normal>low ordering).

### Frontend

- Priority selector + colored badge on note cards, guide library cards, and both
  readers. Sort/filter by priority in the note list + LibraryView. Mirror how
  `status` is already surfaced.

### Verify

- Set/patch priority on a note and a guide → persists, round-trips, filters, sorts.
  A reprocess or an organize sweep leaves `priority` untouched. Invalid value → 422.
