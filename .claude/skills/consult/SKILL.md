---
name: consult
description: Get a design opinion from the internal consultant intern — one khimaira-internal-consultant subagent, no build pipeline. Use when the user types /consult <question> or asks for a consult / design opinion / bug-class enumeration without wanting anything implemented.
---

# /consult <question> — consult-only, no pipeline

Spawn a SINGLE `khimaira-internal-consultant` subagent to analyze the question
and return a design recommendation. This is the lightweight sibling of
`/intern`: consult only — no implementer, no gatekeeper, nothing built.

## Steps

1. If `$ARGUMENTS` is empty, ask the user for the question in one sentence.
2. Announce it in one line: "consulting the internal consultant on <topic>".
3. Invoke the Agent tool:

   ```
   Agent({
     subagent_type: "khimaira-internal-consultant",
     description: "<3-5 word label>",
     prompt: "<the question, plus any grounded context/file paths the user gave>"
   })
   ```

   **Do NOT pass a `model:` override.** The definition pins fable/max; an
   override (e.g. `model: "opus"`) defeats the quota-protection pin and is the
   exact mistake that produced tonight's "opus consultant" confusion. The
   consultant's role definition and tier both come from
   `.claude/agents/khimaira-internal-consultant.md` — let them apply.

4. Relay the consultant's recommendation to the user: the recommended option
   and why, plus the key risks/unknowns it flagged. It RECOMMENDS; you and the
   user decide. Do not auto-implement — if the user wants it built after,
   that's a separate `/intern` or a direct edit.

## Do NOT

- Do NOT use `subagent_type: "Plan"` or `"general-purpose"` and call it a
  consultant — those load neither the consultant role definition nor the fable
  tier. "Consultant" means `khimaira-internal-consultant`, full stop.
- Do NOT reach into the cross-session chat roster (chimera-*, griffin-*, roster
  consultant seats) — those are a separate PRODUCTION roster; onboarding one is
  cross-roster poaching (now blocked daemon-side, but don't attempt it). Your
  consultant is the in-session subagent, not a roster seat.
