# Themis App-Scoped Rules

App-scoped rules let a project add its own Themis invariants without touching the
core rule package. They are **additive only** — they can introduce new constraints
but cannot override, weaken, or remove core rules.

## Location

```
<repo-root>/
  .claude/
    themis/
      agent.yaml        ← app rules for the agent role
      master.yaml       ← app rules for the master role
      intake.yaml       ← (any valid Themis role)
```

Themis walks up from the session's `cwd`, stops at the git root (`.git` dir or
file), and looks for `.claude/themis/<role>.yaml`. No file = core-only (fail-open).

## Naming convention

App rule IDs **must** be namespaced with `APP-<project>-N` to avoid collisions with
core rules. If an app rule has the same `id` as a core rule, the core version wins
and a WARNING is logged.

```
APP-MYAPP-1    ← good
APP-MYAPP-2    ← good
IN-AGENT-1     ← bad — collides with core; core wins, app version silently dropped
```

## Format

App rule files follow the same YAML schema as core Themis rules:

```yaml
role: agent
invariants:
  - id: APP-MYAPP-1
    name: NO_PROD_DEPLOY
    severity: block
    matchers:
      - tool: Bash
        tool_input_field:
          field: command
          pattern: "deploy --env prod"
    message: |
      🛑 Themis APP-MYAPP-1 (NO_PROD_DEPLOY): {tool_name} attempted a production
      deploy. Only release managers may deploy to prod in this repo.
      Contact the release team to deploy.

  - id: APP-MYAPP-2
    name: NO_DIRECT_DB_WRITE
    severity: warn
    matchers:
      - tool: Bash
        tool_input_field:
          field: command
          pattern: "psql.*INSERT|psql.*UPDATE|psql.*DELETE"
    message: |
      ⚠️ Themis APP-MYAPP-2 (NO_DIRECT_DB_WRITE): direct database writes via psql
      are discouraged. Use the migration system or an API endpoint instead.
```

### Supported severity levels

| Value   | Effect |
|---------|--------|
| `block` | Tool call is rejected with the message |
| `warn`  | Tool call is allowed; violation is logged to `themis_violations` |
| `audit` | Tool call is allowed; violation recorded silently for review |

### Supported matchers (same as core)

- `tool: <ToolName>` — match by tool name (e.g. `Bash`, `Edit`, `Write`)
- `tool_input_field.field + pattern` — regex match against a specific input field
- See core rule files in `packages/themis/src/themis/rules/` for the full matcher vocabulary

## Additive-only contract

App rules can only ADD constraints — they cannot relax or remove core rules:

| Action | Supported? |
|--------|------------|
| Add a new rule with a new ID | ✅ Yes |
| Block a tool the core doesn't block | ✅ Yes |
| Override a core rule to be less strict | ❌ No — core wins |
| Remove a core rule | ❌ No — core rules are always included |
| Override a LOCKED core rule | ❌ No — LOCKED rules are immutable even to core overrides |

## How rules are merged

At enforcement time:

1. Core chain is loaded: `universal.base → <role>.base → <role>` (extends-chain merge)
2. App rules file is loaded from `.claude/themis/<role>.yaml`
3. Any app rule whose `id` matches a core rule is dropped (WARNING logged)
4. Surviving app rules are appended after all core rules

## Inspection

To see all rules in effect for your session (including app rules), call:

```python
themis_my_rules(session_id="<your-session-id>", cwd="<your-working-dir>")
```

The `cwd` parameter enables app rule discovery; omitting it returns core rules only.

## Worked example — block accidental prod deploys in a deployment repo

Create `.claude/themis/agent.yaml` at your repo root:

```yaml
role: agent
invariants:
  - id: APP-DEPLOY-1
    name: NO_PROD_DEPLOY_AGENT
    severity: block
    matchers:
      - tool: Bash
        tool_input_field:
          field: command
          pattern: "--env(=|\\s+)(prod|production)"
    message: |
      🛑 Themis APP-DEPLOY-1 (NO_PROD_DEPLOY_AGENT): agent attempted a production
      deploy command. Production deploys require explicit master authorization.
      Use `--env staging` for testing; contact master to trigger a prod deploy.
```

Once the file is in place, any agent in a session whose `cwd` is inside this repo
will have their `Bash` calls matching `--env prod` or `--env production` blocked.

## Tracking the rules in git (when `.claude/` is gitignored)

Many repos gitignore `.claude/` (it holds local session state). If yours does,
`.claude/themis/*.yaml` is **local-only** — enforced for local roster sessions but
not committed, so other clones don't get the gate. For a durable, repo-wide rule,
un-ignore just the `themis/` subtree with a gitignore negation:

```gitignore
# .gitignore — after the existing `.claude/` ignore
!.claude/
!.claude/themis/
!.claude/themis/**
```

Git requires each parent directory to be un-ignored before the leaf — a bare
`!.claude/themis/**` will not work while `.claude/` itself is ignored. This tracks
ONLY the Themis rules while the rest of `.claude/` stays local. Prefer this over
relocating the rules to a non-`.claude` path: the discovery convention stays
identical across apps (`<repo>/.claude/themis/`), so the loader needs no per-app
special-casing.
