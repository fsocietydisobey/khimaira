# Handoff: chimera → khimaira (mid-rename, branch `rename/khimaira`)

This doc captures the state of the chimera→khimaira rename at the moment
this session ends, plus everything needed to pick up cleanly in the next
session. Read this first if you boot fresh in this directory.

## TL;DR

The rename is **80% done in this branch**. Codebase swept, tests
passing on the branch, dotfiles updated, systemd unit renamed, hooks
patched. **Remaining work is runtime cutover** — directory rename,
state migration, MCP re-registration, GitHub repo rename. None of
those were safe to do mid-session, so they were deferred.

## What's done

### In this repo (branch `rename/khimaira`, pushed to origin)

- `packages/chimera/` → `packages/khimaira/` (full directory rename
  with `git mv` preserving history)
- `shared/types/src/chimera_types/` → `khimaira_types/`
- `shared/transport/src/chimera_transport/` → `khimaira_transport/`
- `packages/khimaira/src/khimaira/attach/observer_template/chimera_observer/`
  → `khimaira_observer/` (plus the `.pth` file)
- Empty `apps/monitor-ui/packages/chimera/` scaffolding deleted
- All `pyproject.toml` files: project names, dependencies,
  `[tool.uv.sources]`, entry-point scripts, hatch build targets
- Mass sed sweep across `*.py | *.md | *.yaml | *.yml | *.toml | *.json
  | *.pth | *.txt | *.sh`:
  - `chimera_types` → `khimaira_types`
  - `chimera_transport` → `khimaira_transport`
  - `chimera_observer` → `khimaira_observer`
  - `chimera-types` / `chimera-transport` / `chimera-monitor` /
    `chimera-workspace` / `chimera-profile` → khimaira- equivalents
  - `CHIMERA_*` env vars → `KHIMAIRA_*`
  - `Chimera` → `Khimaira` (title case)
  - `CHIMERA` → `KHIMAIRA` (screaming case)
  - `chimera` → `khimaira` (lowercase — catch-all last)
- 147 tests pass on the renamed codebase
- `uv sync --all-packages` ran clean (chimera packages uninstalled,
  khimaira packages installed)
- Commits: `5a8f6aa` (dispatch), `e7cab2d` (server),
  `cb768ea` (usage), `485e670` (NORTH_STAR), `0e630f4` (baseline
  configurable), `<rename SHA>` (the rename sweep)

### Outside the repo (Joseph's system, on `main`-equivalent state)

- `~/.claude/settings.json` hooks: `python -m chimera.hooks.*` →
  `python -m khimaira.hooks.*` (the python path still points at
  `/home/_3ntropy/dev/chimera/.venv/...` — must update after dir
  rename)
- `~/dotfiles/` swept and committed: `chimera-profile.yaml` →
  `khimaira-profile.yaml`, 12 `chimera-*.md` slash commands renamed
  to `khimaira-*.md`, `claude/CLAUDE.md` + other docs swept,
  `bootstrap.sh` updated
- `~/.bashrc`: `CHIMERA_AUTO_DELEGATE_NUDGE=1` → `KHIMAIRA_*`
- `/home/_3ntropy/.config/systemd/user/chimera-monitor.service` →
  `khimaira-monitor.service` (content swept, file renamed,
  re-enabled, running). Also added `Environment=PATH=...` so npm
  resolves (was a pre-existing latent bug).
- `~/dev/seance/README.md` swept (only sibling repo with a chimera
  reference)

## What's NOT done — the cutover steps

These were deferred because they break the running session if done
in-flight. Do these in order, ideally in a FRESH Claude Code session
booted from the new directory after the dir rename.

### 1. Rename the repo directory

```bash
# From a shell, NOT inside a Claude Code session that's cwd'd here:
mv ~/dev/chimera ~/dev/khimaira
# or: git mv at the parent if you want git to track it (unusual)
```

After this, the `.venv` is at `~/dev/khimaira/.venv/`, and the venv's
editable installs already point at the right paths (uv handles this
since we ran `uv sync` earlier).

### 2. Update settings.json hook command paths

Currently they say `/home/_3ntropy/dev/chimera/.venv/bin/python -m
khimaira.hooks.*`. After the dir rename:

```bash
sed -i 's|/home/_3ntropy/dev/chimera/|/home/_3ntropy/dev/khimaira/|g' \
  /home/_3ntropy/.claude/settings.json
```

A pre-rename backup is at `~/.claude/settings.json.bak.pre-khimaira`.

### 3. Update the systemd service path

Same idea — the unit file currently references the old chimera dir
because we reverted the path mid-session so the daemon could start:

```bash
sed -i 's|/home/_3ntropy/dev/chimera |/home/_3ntropy/dev/khimaira |g' \
  /home/_3ntropy/.config/systemd/user/khimaira-monitor.service
systemctl --user daemon-reload
systemctl --user restart khimaira-monitor.service
```

### 4. Migrate state directories

```bash
mv ~/.local/state/chimera ~/.local/state/khimaira
# if exists:
[ -d ~/.chimera ] && mv ~/.chimera ~/.khimaira
```

The renamed code reads from `~/.local/state/khimaira/` now (sweep
caught the path references). After moving the dir, existing sessions,
handoffs, decisions, usage.jsonl all carry over.

### 5. Re-register the MCP server

```bash
claude mcp remove chimera
claude mcp add khimaira -- bash -lc 'uv --directory ~/dev/khimaira run python -m khimaira.cli mcp 2>>/tmp/khimaira.log'
```

Verify with `claude mcp list` — should show `khimaira` instead of
`chimera`.

Note: in the current session, the MCP server is still the pre-rename
`chimera` process. Tool calls under `mcp__chimera__*` still work for
the lifetime of this session; after restart they become
`mcp__khimaira__*`.

### 6. Rename the GitHub repo

```bash
gh repo rename khimaira -R fsocietydisobey/chimera
# Or via the web UI: Settings → General → Rename repository
# Update local remote:
cd ~/dev/khimaira
git remote set-url origin git@github.com:fsocietydisobey/khimaira.git
```

GitHub auto-redirects old URLs for a while but pin the new name in
docs.

### 7. Merge the rename branch

```bash
cd ~/dev/khimaira
git checkout main
git merge rename/khimaira
git push
git branch -d rename/khimaira
git push origin --delete rename/khimaira
```

Or PR-and-merge if you want the GitHub UI history of the rename as a
PR.

### 8. Migrate Claude Code memory + transcripts (optional)

`~/.claude/projects/-home--3ntropy-dev-chimera/` contains this
session's memory + transcripts. Claude Code auto-creates a new
project dir for `-home--3ntropy-dev-khimaira/` on first session there.
You can either:

- Leave the old memory in place (historical reference)
- Move/copy: `mv ~/.claude/projects/-home--3ntropy-dev-chimera
  ~/.claude/projects/-home--3ntropy-dev-khimaira` (note the dir name
  format mirrors the cwd path)

Memory files inside still reference `chimera` in their content. A
sed sweep handles those if you want consistency.

## State of the open task list

These are the active tasks from the chimera daemon's task store. After
state migration (step 4) they survive into the khimaira session
because the JSONL backing them is in `~/.local/state/{chimera|khimaira}/`.

### Just shipped this session

- `#28` Pool router (capability + cost) ✓
- `#29` Mode field on UsageRecord + plumbing ✓
- `#30` `chimera usage savings` command ✓
- `#31` `mcp__chimera__auto` alias + audit fields on routing log ✓
- `#59` Make `_COUNTERFACTUAL_MODEL` configurable ✓
- `#64` Full rename chimera → khimaira (in progress; this session
  is most of the work)

### Pinned for next sessions (cadence agreed)

- `#63` Daytime work session (morning of 2026-05-14) — was in
  progress when rename interrupted. Remaining items: `#51` README
  update, `#33` Claude Agent SDK investigation, `#60` per-project
  budget, `#54` e2e tests. Plus finishing the rename cutover.
- `#62` Reframe NORTH_STAR + ship install story (evening of
  2026-05-14)

### Major roadmap (from NORTH_STAR.md)

- Phase 0 — Unify MCP registration: **DEFERRED** per Joseph's pragmatic
  call ("bootstrap profile already does this"). The 4 separate MCP
  registrations stay; we add features on top instead.
- Phase 1.0 — MCP-first self-configuration (`setup_*` tools wrapping
  bootstrap/doctor/heal so users configure via chat, not shell)
- Phase 1.1 — Document the chimera protocol (HTTP API + MCP +
  CLI surface)
- Phase 1.2 — Subagent library (`~/.claude/agents/khimaira-*.md`
  curated set, real thinking-token interception in Claude Code)
- Phase 1.3 — PreToolUse interceptor v1 (passive nudge → v2 block
  later)
- Phase 2 — Cross-editor adapter configs (Cursor, Neovim, VS Code,
  aider) in `contrib/`
- Phase 3 — Open-source distribution (PyPI, README rewrite,
  community profile, demo assets)
- Phase 4 — Stretch (Claude Agent SDK, transcript-scrape baseline,
  interceptor v2, web dashboard polish)

### Open operational debt

- `#51` Update README for auto-mode + savings features
- `#52` Rate-limit / quota-exhaustion handling in dispatch path
- `#53` Circuit breakers for repeatedly-failing runners
- `#54` E2E tests for `mcp__khimaira__auto` + delegate
- `#55` Streaming responses through delegate / auto
- `#56` Multi-turn conversation through `mcp__khimaira__auto`
- `#57` `khimaira models sync` — auto-refresh model registry from
  upstream
- `#58` Prompt-caching awareness in cost estimates
- `#60` Per-project model budget for `mcp__khimaira__auto`

### Open decisions

- `#61` PyPI name + license + repo structure — **PARTIALLY
  RESOLVED**: PyPI name = `khimaira` (the rename obviated
  `chimera-orchestrator`). License pick (MIT/Apache/BSD-3) and
  contrib/ vs split repo still open.

## Key files to read first when picking up

1. `NORTH_STAR.md` — vision, phases, principles, anti-goals (last
   reviewed 2026-05-12)
2. `CLAUDE.md` (repo root) — engineering rules captured from real
   bugs we shipped
3. `packages/khimaira/src/khimaira/dispatch/pool_router.py` — the
   routing brain (just shipped this session)
4. `packages/khimaira/src/khimaira/dispatch/registry.py` +
   `default_models.yaml` — the model pool
5. `packages/khimaira/src/khimaira/cli/usage.py` — savings command
   with configurable baseline
6. `packages/khimaira/src/khimaira/server/mcp.py` — `auto` /
   `delegate` MCP tool surface

## Active context

- **Joseph's INTJ-T working style** (from
  `~/dotfiles/claude/rules/personal/approach.md`): prefers
  directness, depth over surface-level, real trade-offs, challenge
  bad ideas. Tools matter — match the energy.
- **Cadence pinned**: morning work = mechanical (commits, small
  features, docs), evening work = strategic (design, deep refactors).
- **Goal**: make khimaira easily integrated into any AI tool. MCP
  is the protocol; editor adapter configs live in `contrib/`, not
  in core.
- **Anti-goals** (do NOT re-argue): build a TUI, build a chimera
  IDE, build per-editor plugins in core, replace Claude Code.
- **The rename motivation**: PyPI `chimera` was taken, forcing
  `-orchestrator` suffix. `khimaira` (Greek original spelling) is
  unique on PyPI, distinctive in search, removes a downstream
  brand-tax that would compound with user growth.

## Sanity checks after cutover

```bash
# In the new ~/dev/khimaira directory:
.venv/bin/khimaira --version            # Should print 'khimaira X.Y.dev...'
.venv/bin/python -c "import khimaira"   # No errors
.venv/bin/khimaira doctor                # Profile + drift check
.venv/bin/python -m pytest packages/khimaira/tests/ -x  # 147+ pass

# Claude Code MCP:
claude mcp list                          # khimaira shown, no 'chimera'
# In a fresh Claude Code session:        # mcp__khimaira__* tools resolve
```

If any sanity check fails, check the troubleshooting log:

- `/tmp/khimaira.log` — MCP server stderr (the bash -lc redirect)
- `journalctl --user -u khimaira-monitor.service -f` — daemon logs
- `~/.local/state/khimaira/khimaira.log` — internal chimera log
  (the file the renamed code writes to)

---

Last updated: 2026-05-13, mid-rename session.
