# Claude Code internal roster design

## Goal

Give a Claude Code master session three native custom subagent types—consultant,
gatekeeper, and implementer—whose tool calls are constrained according to their roles.
The adapter must be removable by deleting only the files introduced for it and removing
one independent `PreToolUse` entry from `~/.claude/settings.json`. It must not modify or
replace `scripts/hooks/themis_pretool.py`.

This is a documentation-derived design. It has not been exercised against a live Claude
Code runtime, hook payload, or subagent transcript in this environment.

## Evidence and confidence

| Claim | Evidence | Confidence |
| --- | --- | --- |
| A hook invocation inside a subagent includes `agent_id` and `agent_type`; for a custom subagent, `agent_type` is the frontmatter `name`, not the filename. | Anthropic's [Hooks reference](https://code.claude.com/docs/en/hooks#common-input-fields) says exactly this in the common input fields table. | **High: confirmed by current public docs; not runtime-verified here.** |
| Multiple matching hooks all run, then Claude Code combines their outcomes; for `PreToolUse`, the most restrictive decision wins (`deny` before `defer`, `ask`, and `allow`). | Anthropic's [hooks guide, “Combine results from multiple hooks”](https://code.claude.com/docs/en/hooks-guide#combine-results-from-multiple-hooks) states this and gives a two-hook example where one denial blocks the call. | **High: confirmed by current public docs; not runtime-verified here.** |
| A command `PreToolUse` hook can block a tool call by exiting with status 2 and explaining the denial on stderr. | Anthropic's [Hooks reference, “Exit code output”](https://code.claude.com/docs/en/hooks#exit-code-output) says exit 2 blocks `PreToolUse`; stdout JSON is ignored on that path and stderr is returned as the error. | **High: confirmed by current public docs; not runtime-verified here.** |
| Project custom subagents live in `.claude/agents/*.md`; their files use YAML frontmatter, the Markdown body is the system prompt, and `name` plus `description` are required. `tools` and `model` are supported. | Anthropic's [custom subagent guide](https://code.claude.com/docs/en/sub-agents#write-subagent-files) documents the location, format, and supported fields. | **High: confirmed by current public docs; not runtime-verified here.** |
| The current public tool name that spawns a subagent is `Agent`. | Anthropic's [Tools reference](https://code.claude.com/docs/en/tools-reference) lists `Agent` as the subagent-spawning tool. | **High for current docs.** The user-facing name `Task` appears to describe an older Claude Code surface; the installed runtime was not inspected or run. |
| The `Agent` tool input selects a custom type through `subagent_type`, alongside `prompt` and `description`. | Anthropic's [Hooks reference, `Agent` tool input](https://code.claude.com/docs/en/hooks#pretooluse-input) documents those fields. | **High: confirmed by current public docs; not runtime-verified here.** |
| A custom subagent can also define hooks in its own frontmatter; those hooks are scoped to that component's lifetime. | Anthropic's [Hooks reference, “Hooks in skills and agents”](https://code.claude.com/docs/en/hooks#hooks-in-skills-and-agents) documents scoped agent hooks. | **High: confirmed by current public docs; not runtime-verified here.** |

The load-bearing attribution and multi-hook composition claims are therefore documented,
not inferred. The remaining uncertainty is version/runtime conformance: this work cannot
prove that the locally installed Claude Code build emits the documented fields or merges
hooks as documented without a live Claude Code test.

## Primary role-attribution design

Each project agent definition has a unique, fixed frontmatter name:

| Definition | `agent_type` expected by the hook | Internal role |
| --- | --- | --- |
| `.claude/agents/khimaira-internal-consultant.md` | `khimaira-internal-consultant` | consultant |
| `.claude/agents/khimaira-internal-gatekeeper.md` | `khimaira-internal-gatekeeper` | gatekeeper |
| `.claude/agents/khimaira-internal-agent.md` | `khimaira-internal-agent` | implementer |

The new settings-level `PreToolUse` hook receives every proposed tool call and resolves
the role only from the exact `agent_type` value. The main thread normally has no
`agent_type`, so the roster hook makes no decision. Other built-in or custom subagent
types likewise fall through unchanged. An unknown value beginning with the reserved
`khimaira-internal-` prefix is denied so a typo or half-installed roster definition does
not create an apparently governed but actually ungoverned role.

The docs also say `agent_type` may be present when a whole session is launched with
`--agent`. Consequently, the same policy will intentionally apply if one of these exact
roster types is used as a top-level `--agent`, not only when spawned by the master.

`agent_id` is used only as a consistency signal. If an exact roster `agent_type` is
present, the role is governed whether or not `agent_id` is present. If `agent_id` is
present without `agent_type`, the hook logs a diagnostic and takes no decision because
it cannot safely distinguish an incompatible runtime's roster agent from an unrelated
subagent. That is a deliberate fail-open compatibility choice, not a security claim.

## Governance policy

The role prompts are condensed from
`packages/khimaira/src/khimaira/roles/{consultant,gatekeeper,agent}.md`. Prompts describe
the behavioral contract; the hook enforces the deterministic subset.

| Role | Tool exposure in agent definition | Hook-enforced denials |
| --- | --- | --- |
| consultant | Read/search/web inspection plus Bash | File-edit tools; nested `Agent`/legacy `Task`; mutating Bash, including git-state changes, common filesystem mutation, and non-`/tmp` output redirection. |
| gatekeeper | Read/search/web inspection plus Bash | File-edit tools; nested `Agent`/legacy `Task`; mutating Bash. This internal roster deliberately makes gatekeeper fully review-only, stricter than the production role's test-only edit allowance, matching the requested “reviews instead of fixes” boundary. |
| implementer | Read/search/web tools, file-edit tools, and Bash | Nested `Agent`/legacy `Task`; git-state-changing subcommands, including `add`, `commit`, `push`, `merge`, `rebase`, `reset`, `checkout`, `switch`, `restore`, `clean`, `stash`, tag/branch/worktree/ref mutations, and hook bypass via `--no-verify`. |

Tool lists provide least-privilege ergonomics, while the hook repeats the important
denials so accidentally broadening a future list does not silently remove governance.
The hook inspects the Bash command text conservatively. It is a guardrail, not a shell
sandbox: sufficiently indirect execution or obfuscation may evade text matching. The
prompt and normal Claude Code permissions remain additional layers, but this design does
not claim adversarial containment.

All denials use command-hook exit status 2 and a role-specific message on stderr. Allowed
or unrelated calls exit 0 without an allow decision, preserving the existing production
hook and Claude Code's normal permission flow. Because documented composition selects the
most restrictive result, either the existing `themis_pretool.py` hook or this roster hook
can independently deny a call.

## Fallback if the installed runtime lacks usable `agent_type`

Do not infer roles from transcript paths, prompt text, filenames, or task descriptions.
Those mechanisms are undocumented and brittle.

The best documented fallback is to put a scoped `PreToolUse` hook in each custom agent's
frontmatter and invoke the same script with an explicit fixed `--role` argument. Anthropic
documents that agent-frontmatter hooks run only while that component is active. This
would avoid relying on payload attribution, but it changes the deployment mechanism and
must be applied only after a controlled live test proves `agent_type` unavailable. It is
not enabled in the initial files, so there is one role source of truth and no untested
dual path.

If neither settings-level `agent_type` nor scoped agent hooks work in the installed
version, the roster cannot be called governed. The safe operational response is to remove
the settings entry and agent files or upgrade Claude Code—not to claim prompt-only role
instructions are enforcement.

## Proposed deployment (not applied)

Add a second object to the existing `hooks.PreToolUse` array in
`~/.claude/settings.json`; leave the current `themis_pretool.py` object byte-for-byte
unchanged:

```diff
 "PreToolUse": [
   {
     "hooks": [
       {
         "type": "command",
         "command": "/home/_3ntropy/dev/khimaira/.venv/bin/python3 /home/_3ntropy/dev/khimaira/scripts/hooks/themis_pretool.py"
       }
     ]
+  },
+  {
+    "hooks": [
+      {
+        "type": "command",
+        "command": "/home/_3ntropy/dev/khimaira/.venv/bin/python3 /home/_3ntropy/dev/khimaira/packages/khimaira/src/khimaira/hooks/claude_internal_roster_pretool.py"
+      }
+    ]
   }
 ]
```

No matcher is set, so the hook sees all tool names and performs role-aware filtering
itself. This diff is a proposal only and must not be applied from this session.

Because this repository did not previously have `.claude/agents/`, Anthropic's custom
subagent guide says a Claude Code session that was already running when the directory was
created will need a restart before it discovers these definitions. That operational
restart is also unverified here.

## Verification boundary

Local unit tests can prove JSON parsing, exact `agent_type` mapping, policy decisions,
exit codes, and stderr messages by invoking the script as a subprocess. They cannot prove
that Claude Code passes those fields, discovers the project agents, runs both settings
hooks, or merges their decisions. A later live validation should use Claude Code's
read-only `/hooks` view, then attempt one allowed and one denied call per role. That live
validation is explicitly out of scope here.
