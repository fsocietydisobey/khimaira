# Installing khimaira

Three install paths from quickest-to-most-personalized.

## Path 1 — One-shot via `uvx` (fastest)

If you have [`uv`](https://docs.astral.sh/uv/) installed, you can run
khimaira's bootstrap without cloning anything first:

```bash
uvx --from git+https://github.com/fsocietydisobey/khimaira khimaira bootstrap \
    --profile https://raw.githubusercontent.com/fsocietydisobey/khimaira/main/khimaira-profile.community.yaml
```

This:

1. Clones the khimaira repo to `~/dev/khimaira` and runs `uv sync --all-packages`
2. Registers khimaira as an MCP server with Claude Code (user scope)
3. Writes khimaira's hooks to `~/.claude/settings.json` (backup saved)
4. Installs the host-native supervisor (systemd user unit on Linux; macOS users run `khimaira monitor watch` themselves)
5. Builds the dashboard SPA at `http://localhost:8740/`

Re-running is idempotent — safe to invoke from cron, on machine
upgrade, or whenever you've pulled new khimaira code.

## Path 2 — Clone + bootstrap

If you'd rather clone first so you can poke at the source:

```bash
git clone https://github.com/fsocietydisobey/khimaira.git ~/dev/khimaira
cd ~/dev/khimaira
uv sync --all-packages
uv run khimaira bootstrap --profile khimaira-profile.community.yaml
```

Same result as Path 1, more transparent.

## Path 3 — Personalized profile

Copy the community profile into your own dotfiles repo and customize:

```bash
cp khimaira-profile.community.yaml ~/dotfiles/khimaira-profile.yaml
# Edit ~/dotfiles/khimaira-profile.yaml — add your dotfiles repo,
# sibling MCP servers you maintain, additional symlinks, etc.

uv run khimaira bootstrap --profile ~/dotfiles/khimaira-profile.yaml
```

The portable agent setup pattern: same profile applied on N machines
yields N matching environments. Re-run `khimaira sync` on any machine
to pick up profile changes (new symlinks, new MCP servers, etc.).

For a fully-decorated example with dotfiles + sibling repos +
project-specific MCP servers, see
[`tasks/bootstrap-profile/EXAMPLE-PROFILE.yaml`](../tasks/bootstrap-profile/EXAMPLE-PROFILE.yaml).

## Verifying the install

After any bootstrap run, this should "just work":

```bash
khimaira doctor          # diagnose environment — daemon up? hooks current? supervisor active?
khimaira tools           # discover every khimaira surface (CLI, MCP tools, slash commands, web routes)
khimaira usage savings   # see savings (empty if you haven't dispatched anything yet)
```

In a fresh Claude Code session, this should appear automatically (via
the SessionStart hook khimaira just wrote):

```
🆔 khimaira session_id: `...`
```

If the `mcp__khimaira__*` tools aren't visible after install, that's
the [MCP catalog gap](#mcp-catalog-gap-after-install) — restart Claude
Code once and they'll appear.

## What `bootstrap` writes (so you know what's changing)

| Path | What |
|---|---|
| `~/dev/khimaira` | The khimaira workspace clone (skip if cloned already) |
| `~/.claude/settings.json` | khimaira's four hooks merged in (backup at `.bak.<mtime>`) |
| `~/.config/systemd/user/khimaira-monitor.service` | systemd user unit (Linux only) |
| `~/.local/state/khimaira/` | Daemon state (sessions, handoffs, usage, etc.). Created at first daemon start. |
| `~/.khimaira/` | User config — `models.yaml`, optional `task_sources.yaml`, optional `todo.jsonl`. Not created by bootstrap; you add files here as you customize. |

No files written outside these paths.

## Optional setup

### Task sources

khimaira's SessionStart hook surfaces tasks assigned to you, but it
doesn't prescribe where they live. The default config reads
`~/.khimaira/todo.jsonl` — drop tasks in there and they appear on
next boot:

```bash
mkdir -p ~/.khimaira
cat >> ~/.khimaira/todo.jsonl <<EOF
{"id": "TODO-1", "title": "fix the auth bug", "state": "in-progress"}
{"id": "TODO-2", "title": "write the README", "state": "todo"}
EOF
```

To wire in a different tracker (Linear, GitHub Issues, Jira), see
[`tasks/task-sources/IMPLEMENTATION.md`](../tasks/task-sources/IMPLEMENTATION.md)
for the adapter contract.

### Model registry

The dispatch pool reads `~/.khimaira/models.yaml`. Bootstrap doesn't
create this — khimaira ships sensible defaults at
`packages/khimaira/src/khimaira/dispatch/default_models.yaml` and
falls back to them when no user file exists. Customize via:

```bash
khimaira models sync           # diff your file against shipped defaults
khimaira models sync --apply   # apply shipped defaults, preserving any user-added entries
```

## khimaira-chat MCP server

khimaira ships **two** MCP servers. Bootstrap registers both automatically, but if you're configuring manually or debugging:

```bash
# The main orchestration server (session state, pipelines, semantic search, etc.)
claude mcp add khimaira -- uv --directory ~/dev/khimaira run khimaira mcp

# The per-session chat server (real-time cross-session channels)
claude mcp add khimaira-chat -- uv --directory ~/dev/khimaira run python -m khimaira_chat.server
```

`khimaira-chat` is a stdio subprocess that auto-registers with the `khimaira-monitor` daemon at startup and opens an SSE stream for push-delivered chat events. If you see `mcp__khimaira-chat__*` tools missing, verify both entries appear in `claude mcp list`.

### Session bootstrap for multi-agent workflows

When spawning a roster of role-typed sessions (master + agents + critic):

1. Open each Claude Code session and `/rename` it to its role-name (e.g., `agent-1`, `agent-2`, `critic-1`)
2. In the master session, run `/khimaira-bootstrap-roster` to onboard all sessions into a shared chat with correct roles assigned
3. Alternatively, run `/khimaira-orchestrate <peers...> <scope>` to create the chat and send a brief in one command

See `docs/khimaira-chat.md` for the full multi-agent protocol.

## MCP catalog gap after install

The most common friction point. Claude Code snapshots its MCP catalog
at session start. If khimaira gets registered MID-session (e.g.
bootstrap runs while Claude Code is open), the new tools don't appear
until Claude Code restarts.

Fix: quit Claude Code, reopen. `claude mcp list` should show
`khimaira` after the restart; `mcp__khimaira__*` tools become callable
in the new session.

## Updating

```bash
cd ~/dev/khimaira && git pull && uv sync --all-packages
uv run khimaira sync   # re-apply profile (symlinks, hooks, supervisor)
```

If hook code changed in the new khimaira version, restart Claude Code
so the new hook scripts get loaded. The bootstrap profile's
`install_claude_hooks: true` ensures `~/.claude/settings.json` stays
pointed at the right path; `khimaira sync` re-runs the merge.

## Uninstall

```bash
uv run khimaira install-hooks --uninstall  # remove from ~/.claude/settings.json
systemctl --user disable --now khimaira-monitor  # Linux supervisor
claude mcp remove khimaira                   # Claude Code MCP registration
rm -rf ~/dev/khimaira ~/.local/state/khimaira ~/.khimaira
```
