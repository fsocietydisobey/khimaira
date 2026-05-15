# khimaira launch plan

> Open-source + write-up + Anthropic outreach. Path E (startup) deferred but
> kept open. Captured 2026-05-15 by khimaira-21 from a strategy conversation
> with Joseph. Living doc — update as decisions land.

## Why this exists

khimaira has crossed a threshold. After today's session it includes a working
multi-agent orchestration layer (Phase B v1.0 → v1.3) with documented
patterns and 491 passing tests. The system has been used to author its own
features through 6+ rounds of dogfood collaboration — the agents authored the
agent infrastructure.

This is genuinely novel work. The launch plan below converts it from "Joseph's
private tool" into "something the broader Claude Code / AI-tooling community
encounters and reacts to."

## Current state (assets we already have)

- ✅ **PyPI packages live**: khimaira v0.1.2, khimaira-types v0.1.0,
  khimaira-transport v0.1.0, khimaira-chat v0.1.0, khimaira-specter v0.2.0,
  khimaira-scarlet v0.2.0. Phase 2 of cascade (khimaira-seance + khimaira-sibyl
  v0.2.0) auto-fires tomorrow at 00:05 UTC.
- ✅ **Working multi-agent infrastructure**: chats, structured tasks,
  master/agent roles, session transfer with chat-membership inheritance.
- ✅ **`docs/khimaira-chat.md`**: ~700-line reference doc, co-authored by three
  Claude sessions through the chat mechanism it documents.
- ✅ **Rich git history**: ~30 Phase B commits with detailed messages.
- ✅ **Working slash commands**: `/khimaira-chat`, `/khimaira-orchestrate`,
  `/khimaira-transfer-session`, etc. in `~/dotfiles/claude/commands/`.
- ✅ **Survives suspend/resume**: SSE keepalive + subscriber liveness + eager
  registration all shipped.

## Strategic context (the open-source question)

Resolved decisions (see session_log_decision):

- **Open-source is the right path.** Code is the byproduct, not the asset.
  Real value lives in design judgment + ongoing iteration + reputation as
  originator — none of which is in any source file.
- **License: MIT initially.** Maximum adoption, lowest friction. Relicense to
  BSL later if pivoting to startup mode.
- **Path E (startup) deferred.** Keep it open by not making
  irreversible-toward-pure-commodity decisions. AGPL or BSL would be the hedge
  if we change our mind.

What devs *cannot* steal even with full source access:
- Taste / opinionated design choices (master = creator, kind=msg with meta
  filters not new top-level kinds, JSONL not SQLite, etc.) — these live in
  Joseph's head, not in the code.
- Ability to keep evolving the design (8 versions shipped today).
- Reputation as originator + brand.
- Project-specific integration (jeevy_portal observers, etc. — not shipped).

What they *can* take:
- The literal code (reimplementable in a week).
- The patterns (these leak via the launch post anyway).

The real risk isn't theft. The real risk is publishing without strategy.
Open-sourcing as part of an active project compounds; open-sourcing then
walking away erodes. The four-week plan below assumes active engagement.

## The four-week sequence

### Week 1 — Foundation (~6 hours total)

**Day 1-2: License + repo polish**

- [ ] Add `LICENSE` file at repo root (MIT). Same for khimaira-chat,
      khimaira-types, khimaira-transport package roots.
- [ ] Rewrite `README.md` to lead with the experience, not the feature list.
      Suggested opener:

      > **khimaira is a multi-agent orchestration layer for Claude Code that
      > lets your Claude sessions talk to each other in real time, delegate
      > structured tasks, and hand off context when one session's window fills
      > up. It's been used to author its own multi-agent features through 6
      > rounds of dogfood collaboration.**

- [ ] Ensure `docs/khimaira-chat.md` is the canonical entry point linked from
      the README.
- [ ] Add a `CONTRIBUTING.md` (brief). Sets the tone: "Phase B v1.4 is open —
      submit RFCs first for architectural changes."
- [ ] Tag `v0.2.0` of khimaira-chat after Round 6 commit lands (already does:
      `bd7f1af`). Push the tag.

**Day 3-5: Write the launch post**

This is the most important artifact in the entire plan. Spend real time here.

- **Title options** (pick after drafting):
  - *"I watched three Claude sessions ship code together for 8 hours. Here's how."*
  - *"Multi-agent code authoring with Claude Code: 6 rounds of dogfood patterns."*
  - *"How to make Claude sessions talk to each other (and why you'd want to)."*

- **Structure** (target ~2500 words):
  1. **Hook (200 words)**: Open with the actual scene. test-master finishes a
     lane, test-agent peer-reviews without being asked, khimaira-21 assembles,
     Phase B v1.1.a ships. None of it required Joseph to type — it happened in
     chat, in real time, between Claude sessions.
  2. **What this is (300 words)**: orchestration primitive + Anthropic's
     `claude/channel` (research preview, v2.1.80+) + MCP. Cite Anthropic
     explicitly.
  3. **The patterns (1500 words)**: master/agent roles, structured tasks with
     lifecycle, `chat_transfer_membership` for session handoff, auto-accept
     allowlists, SSE keepalive for survive-suspend, eager registration for
     idle sessions. Quote agent transcripts as evidence.
  4. **What went wrong (500 words)**: lazy-registration bug for idle sessions,
     SSE silent death post-suspend, creator-role not propagating during
     transfer. Show the iteration. Honesty is credibility.
  5. **What's next (300 words)**: v1.4 ideas, Phase C trust+identity,
     speculation on where Anthropic might productize.
  6. **Try it (100 words)**: `uv tool install khimaira`. GitHub. Issue tracker.

- **Where to publish**: own blog if you have one; otherwise Substack or
  Hashnode. **Avoid Medium** — they gate readership.

**Day 6-7: Tighten + ship**

- [ ] Sit on the draft a day, re-read with fresh eyes
- [ ] Add 1-2 Mermaid diagrams (reuse from `docs/khimaira-chat.md`)
- [ ] Target ≤2500 words for HN attention span
- [ ] Add a pull-quote near the top that's screenshot-able

### Week 2 — Distribution

**Tuesday or Wednesday morning Pacific** (best HN window):

- [ ] HN: title `Show HN: Khimaira — Multi-agent chat orchestration for Claude Code`
- [ ] Lobsters with `practices` + `ai` tags
- [ ] r/MachineLearning with `[P]` project flair
- [ ] Skip r/programming unless the post is broadly accessible

**Same day on Twitter/X:**

- [ ] Thread of 6-8 tweets. First tweet = hook screenshot. Last tweet = post link.
- [ ] Tag `@AnthropicAI`, `@alexalbert__`, `@maheshmurag`. Do not @-spam beyond
      that — Anthropic dev rel watches their mentions.

**LinkedIn** (optional, for recruiting reach):

- [ ] Condensed version, link to full piece. Tag AI-tooling builders you know.

**First 48 hours**:

- [ ] Reply to substantive comments thoughtfully
- [ ] Don't argue with bad-faith trolls
- [ ] Note "interesting question worth a post of its own" for follow-up

### Week 3 — Anthropic engagement

**Do not reach out cold before the post lands.** Wait for them to come to you,
or reach out after evidence of the work exists publicly.

After the post:

- [ ] File GitHub issues on `anthropics/claude-code` for things we discovered:
      - Channel notifications don't always trigger turn cycles on idle windows
      - MCP subprocess respawn brittleness on package upgrade
      - tools/list_changed doesn't fix subprocess-stale-code
      Each issue: short, reproducible, links to the post for context.
- [ ] Submit feedback via [anthropic.com/contact](https://anthropic.com/contact)
      with framing: *"I built this on your research-preview channels capability
      and would love to share what we learned."* Under 200 words. Link to post.
- [ ] If they reach out: be generous with time. Possible outcomes include a
      call, an invitation to feature the work, or just thanks. Either way, the
      relationship is established.

### Week 4 — Decide what comes next

By now there's signal. Match it to a path:

| Signal | Path |
|---|---|
| Lots of stars, modest engagement | Keep maintaining, deep post in 1-2 months |
| Anthropic dev rel reaches out, asks for a call | Schedule it, be open to collaboration |
| Multiple "exactly what we need at $COMPANY" responses | Consulting or wedge product |
| One or two companies want license / support | Soft consulting, slow pre-seed |
| Crickets | Patterns still proven. Iterate again, post again. Compounds. |

## Concrete day-1 todos (when you're back at the keyboard)

1. **Decide on the license**: MIT is the default recommendation; relicense later
   if needed.
2. **Rename the GitHub repo description** if it's still generic.
3. **Sketch the post title** that makes you want to write it.
4. **Spend an hour on the opening 200 words.** Once that's drafted, the rest
   becomes easier. The hardest part of any launch isn't the launch — it's
   writing the thing that goes in the launch.

## Talking points to weave into the post

- **The token-multiplier tradeoff is the feature, not a bug.** Multi-agent
  systems use 4-6× the tokens of single-agent for the same task. They produce
  meaningfully better output via peer review. The math is honest; readers will
  ask, and the answer is "yes, it costs more — that's how peer review works in
  any system, including human teams." Anthropic will appreciate the candor.
- **`claude/channel` is research preview, not stable.** This is groundwork for
  whatever they ship next. Your usage data is the kind of evidence they want.
- **The pattern composes.** Each Phase B feature (v1.0 routing, v1.1 tasks,
  v1.2 transfer, v1.3 liveness) was authored by the agents using the prior
  feature. Recursive infrastructure deployment.
- **Survives real production conditions.** Laptop suspend/resume, daemon
  restarts, subprocess crashes, network blips — all tested, all handled.
- **The orchestration patterns are deliberately opinionated.** Master/agent
  roles via implicit-from-creator, structured tasks with strict lifecycle,
  push-via-channel-not-pull. Other tools (CrewAI, LangGraph, Swarm, AutoGen)
  chose different opinions. The diversity is healthy; the post should explain
  the choices without claiming they're universal.

## Open design questions (deferred)

- **License: MIT vs Apache 2.0 vs BSL.** MIT picked for week-1 launch. Revisit
  if pivoting to startup mode.
- **Whether to formally request Anthropic featurization.** Decide after the
  post lands and we see if dev rel reaches out organically.
- **Whether to build any of: docs site, demo video, podcast appearance.** All
  high-leverage but expensive in time. Decide based on week-2 reception.
- **Phase C (trust + identity).** Cryptographic peer identity for auto-accept
  + cross-machine federation. Speculative; deferred until the multi-machine
  use case is real.
- **Phase B v1.4 (`chat_set_creator`).** Admin-style retroactive role
  propagation for `chat_transfer_membership`. test-master surfaced this during
  Round 6 Lane E. Deferred to next round.
- **Model + thinking-mode per role.** New idea from Joseph 2026-05-15
  (extended same day): assign Opus + ultrathink to master/orchestrator, Sonnet
  + short-think to implementing agents, Haiku + no-think to observers.
  Mitigates rate-limit blowups during dogfood rounds (today's session burned
  through usage cap mid-flow). Two dimensions compound:
  - Model: ~5× difference Opus→Sonnet, ~10-20× Opus→Haiku
  - Thinking budget: ~10-50× difference between max-thinking and no-thinking
    AT THE SAME MODEL. Thinking mode is arguably the larger lever.

  Recommended budget table:

  | Role | Model | Thinking |
  |---|---|---|
  | Master / orchestrator | Opus 4.7 | ultrathink / think harder |
  | Implementing agent | Sonnet 4.6 | think (short budget) |
  | Observer / peer-reviewer | Haiku 4.5 | none / default |

  Implementation options: (a) session-level preference in `status.json`,
  (b) task-level `recommended_model` + `recommended_thinking` hint on
  `chat_task_create`, (c) role-based defaults implicit unless overridden,
  surfaced as convention in the templated brief.

  Option (c) is cleanest; maps to existing role gating without MCP changes.
  Relayed to khimaira-0 for next round consideration. Concrete deliverable:
  update `khimaira-orchestrate.md` + `khimaira-transfer-session.md`
  templates to include both dimensions, plus a "Token-cost budgeting"
  section in `docs/khimaira-chat.md`.

## Decision log entries (already captured in khimaira session state)

- `e7e39f474681` (2026-05-15) — Open-source + write-up + Anthropic outreach
  chosen as go-to-market. Path E deferred but kept open.
- `ffb89a464301` — First organic test of `chat_transfer_membership`: 7 chats
  transferred from khimaira-21 to khimaira-0 in parallel without errors.
- `757d1055d18b` — Initiated session transfer to khimaira-0; first organic
  exercise of Phase B v1.2's chat_transfer_membership flow.
- `e60dc76216b9` — PyPI cascade phase 1 fired manually; all 3 packages
  published cleanly.

## References

- `docs/khimaira-chat.md` — full reference doc (the launch post is the
  popularized version of this)
- `tasks/khimaira-chat/PHASE-B-VISION.md` — design history
- Anthropic channels reference:
  [code.claude.com/docs/en/channels.md](https://code.claude.com/docs/en/channels.md)
- Recent Phase B commits: `bd7f1af`, `7cfc2d6`, `89b93ac`, `79a5611`,
  `0b01a44`, `7148de7`, `a78188b`, `4ab7d3e`, `10baa6d`
