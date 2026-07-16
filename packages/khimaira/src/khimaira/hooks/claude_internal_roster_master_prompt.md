# Claude Code internal roster: master prompting note

## Recommended budget

| Role | Model | Effort |
| --- | --- | --- |
| master (the untyped top-level session — no `.claude/agents/*.md` file, set via slash commands) | latest Opus, 1M context | high |
| `khimaira-internal-consultant` | Sonnet 5, 1M context | max |
| `khimaira-internal-gatekeeper` | Sonnet 5, 1M context | high |
| `khimaira-internal-agent` | Sonnet 5, 1M context | (unset — inherits default) |

Consultant/gatekeeper/agent are pinned in each `.claude/agents/khimaira-internal-*.md`
file's own frontmatter (`model:`/`effort:`) — they apply automatically whenever the master
spawns that type, no manual step needed. The master session itself has no such file (it's
the plain top-level session, not a spawned subagent), so its budget has to be set directly
in that session: type `/model opus[1m]` then `/effort high`.

The master remains the only orchestrator. Delegate through Claude Code's current native
`Agent` tool (called `Task` in older discussions), using one of these exact custom types:

If `.claude/agents/` did not exist when the current Claude Code session started, restart
that session after installing these files. Anthropic documents that the file watcher does
not discover the first newly created agents directory in an already-running session.

| Need | `subagent_type` | What the master supplies |
| --- | --- | --- |
| Resolve ambiguity or design | `khimaira-internal-consultant` | The decision to make, relevant constraints/files, known evidence, and the required output. |
| Implement a decided unit | `khimaira-internal-agent` | A bounded task, acceptance criteria, file/scope ownership, and verification expectations. Spawn multiple instances of this same type for independent work units. |
| Review a completed change | `khimaira-internal-gatekeeper` | The intended contract, exact diff/scope, tests already run, and whether independent review is required. |

Anthropic's current [hook input reference](https://code.claude.com/docs/en/hooks#pretooluse-input)
documents the `Agent` tool input fields `prompt`, `description`, `subagent_type`, and an
optional model override. In normal conversation, ask Claude directly, for example:

```text
Use the khimaira-internal-consultant agent in the foreground. Decide whether the parser
should reject or normalize duplicate keys. Read parser.py and its callers, compare two or
three options, recommend one, and return testable acceptance criteria. Do not implement.
```

The corresponding role-bearing tool input must select the exact type:

```json
{
  "description": "Decide duplicate-key behavior",
  "prompt": "<the fully framed assignment>",
  "subagent_type": "khimaira-internal-consultant"
}
```

Do not encode the role only in `prompt`. Role assignment comes from the selected custom
agent definition: its frontmatter `name` becomes `agent_type` on that subagent's hook
calls, and the independent roster hook maps that exact value to policy. A typo beginning
with `khimaira-internal-` is denied by the hook; a completely unrelated agent type is
outside this adapter's scope.

Recommended master flow:

1. Consult only when requirements or design are unresolved.
2. Give each implementer a concrete, non-overlapping assignment. The master owns any
   coordination between multiple instances.
3. After implementation, invoke a fresh gatekeeper with the contract, diff, and reported
   tests. Route HOLD findings back to an implementer; invoke a fresh gatekeeper after
   rework when independence matters.
4. Only the master decides whether to commit after a SHIP verdict. This adapter blocks
   implementer commits but does not impose a commit gate on the untyped master thread.

This prompting note does not prove live behavior. The design and field mapping are based
on Anthropic's public documentation and require later validation in an actual Claude Code
session.
