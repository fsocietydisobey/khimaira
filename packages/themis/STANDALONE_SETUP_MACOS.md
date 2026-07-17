# Standalone internal roster setup on macOS

This installs the three Claude Code agent definitions and the local Themis
`PreToolUse` hook. It does not configure khimaira's daemon, dotfiles, MCP
servers, or cross-session chat.

## Recommended standalone install

Install the core package directly from this repository into a dedicated virtual
environment:

```bash
THEMIS_VENV="$HOME/.local/share/khimaira-themis"
python3.12 -m venv "$THEMIS_VENV"
"$THEMIS_VENV/bin/python" -m pip install \
  "git+https://github.com/fsocietydisobey/khimaira.git@main#subdirectory=packages/themis"
"$THEMIS_VENV/bin/themis" install-internal-roster
```

The Git direct reference is intentional: this guide does not assume that
`khimaira-themis` has been published to a package index. Pin `@main` to a tag or
commit when reproducibility requires an immutable revision.

The command copies these package assets into `~/.claude/agents/`:

- `khimaira-internal-consultant.md`
- `khimaira-internal-gatekeeper.md`
- `khimaira-internal-agent.md`

It also merges one independent entry into
`~/.claude/settings.json`:

```text
<themis tool Python> -m themis.hooks.claude_internal_roster_pretool
```

The interpreter path is captured from `sys.executable` and shell-quoted, so a
macOS home or virtual-environment path containing spaces remains one argument.
Existing unrelated settings and hooks are preserved. If an agent definition
already exists with different content, installation refuses before writing
anything; inspect the difference, then use `--force` only if replacing it is
intentional.

Run the command again to verify idempotence. Every line should report
`unchanged`. Restart Claude Code after installation.

To remove only standalone-Themis-owned content:

```bash
"$THEMIS_VENV/bin/themis" install-internal-roster --uninstall
```

An agent file modified after installation is preserved during uninstall.

## Integrated khimaira CLI form

If full khimaira is installed, use its existing hook installer instead:

```bash
khimaira install-hooks
```

That command installs the integrated
`khimaira.hooks.claude_internal_roster_pretool` hook plus khimaira's other
lifecycle hooks. The standalone installer recognizes that module as the same
roster-hook namespace and will not add a duplicate. `khimaira install-hooks`
does not copy the three custom agent definitions; copy or symlink them using the
next section, or use `themis install-internal-roster` once after the integrated
hook is present. In that case Themis installs the missing agents and leaves the
integrated hook untouched.

## Manual agent copy or symlink

For the dedicated virtual environment above, find the package-contained assets
with:

```bash
THEMIS_VENV="$HOME/.local/share/khimaira-themis"
THEMIS_PYTHON="$THEMIS_VENV/bin/python"
ASSET_DIR="$("$THEMIS_PYTHON" -c 'from importlib import resources; print(resources.files("themis").joinpath("assets", "claude_agents"))')"
mkdir -p "$HOME/.claude/agents"
```

Copy them:

```bash
cp "$ASSET_DIR"/khimaira-internal-{consultant,gatekeeper,agent}.md "$HOME/.claude/agents/"
```

For project-only discovery, use the repository's agent directory instead:

```bash
mkdir -p "$PWD/.claude/agents"
cp "$ASSET_DIR"/khimaira-internal-{consultant,gatekeeper,agent}.md "$PWD/.claude/agents/"
```

The installer can likewise target project-local Claude configuration without
touching the global files:

```bash
"$THEMIS_VENV/bin/themis" install-internal-roster \
  --claude-settings "$PWD/.claude/settings.json" \
  --claude-agents-dir "$PWD/.claude/agents"
```

Or, when you explicitly want updates to track the installed package, create
symlinks after confirming those destinations do not already exist:

```bash
for role in consultant gatekeeper agent; do
  ln -s "$ASSET_DIR/khimaira-internal-$role.md" \
    "$HOME/.claude/agents/khimaira-internal-$role.md"
done
```

Do not use `ln -sf` blindly: it can replace a user-maintained definition.

## Manual Claude settings JSON

First print the exact interpreter path:

```bash
THEMIS_VENV="$HOME/.local/share/khimaira-themis"
THEMIS_PYTHON="$THEMIS_VENV/bin/python"
printf '%s\n' "$THEMIS_PYTHON"
```

Merge this as an additional `PreToolUse` array entry in
`~/.claude/settings.json`, replacing `/ABSOLUTE/PATH/TO/python` with that output.
If the path contains spaces, retain the single quotes shown around it.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/ABSOLUTE/PATH/TO/python' -m themis.hooks.claude_internal_roster_pretool",
            "_themis_hook": "themis.hooks.claude_internal_roster_pretool"
          }
        ]
      }
    ]
  }
}
```

This is a merge fragment, not a replacement file. Preserve every existing key
and existing hook entry. If
`khimaira.hooks.claude_internal_roster_pretool` is already present, do not add
the standalone entry.

For an integrated khimaira installation, the corresponding manual merge
fragment is instead:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/ABSOLUTE/PATH/TO/python' -m khimaira.hooks.claude_internal_roster_pretool",
            "_khimaira_hook": "claude_internal_roster_pretool"
          }
        ]
      }
    ]
  }
}
```

Use the interpreter that owns the `khimaira` installation. Do not register
both module forms: they govern the same roster-hook namespace.

## Optional Codex hook

Standalone Codex hook wiring is available, but it does not install khimaira's
AGENTS.md, chat skills, or MCP servers:

```bash
"$THEMIS_VENV/bin/themis" install-internal-roster --codex
```

It merges the following shape into `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "'/ABSOLUTE/PATH/TO/python' -m themis.hooks.codex_pretool",
            "statusMessage": "Themis internal-roster check"
          }
        ]
      }
    ]
  }
}
```

The installer treats an existing `khimaira.hooks.codex_pretool` command as the
same namespace and does not duplicate it. Codex role attribution depends on the
local rollout metadata written for spawned agents; unresolved attribution fails
open with a diagnostic.

## Evidence and runtime limitations

- This repository environment does not provide an actual macOS runtime or a
  live Claude Code/Codex hook process. The macOS commands and path handling were
  inspected statically and exercised with scratch paths, including paths with
  spaces; that is portability evidence, not macOS runtime proof.
- Claude's public hook documentation describes `agent_type` on subagent hook
  payloads and says that all matching hooks run. On macOS and Linux it runs
  command hooks through `sh -c`, which is why the POSIX shell quoting of
  the interpreter path is load-bearing. See Anthropic's
  [hooks reference](https://code.claude.com/docs/en/hooks) and
  [subagent documentation](https://code.claude.com/docs/en/sub-agents). This
  setup has not directly observed a live Claude Code subagent transcript or
  hook payload here.
- Codex tests exercise the documented/local rollout-file shape, but this guide
  does not claim that every Codex release preserves that private on-disk layout.
- Local Themis evaluation deliberately has no daemon enrichment. Matcher-only
  catalog rules and locally knowable conditions run; conditions requiring live
  roster/chat state remain inactive. Commit/approval gates use the catalog's
  explicit enrichment-error sentinel and fail closed for those operations.
- A successful file merge proves configuration state, not that a currently
  running coding-tool session reloaded it. Restart the tool and perform a safe
  blocked-call probe before relying on the roster for real work.
