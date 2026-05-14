# khimaira sync — pip-install mode awareness (v1.2)

**Status**: spec landed 2026-05-14 by `chimera-extension` (2cac13b6) after Joseph flagged the OSS-launch gap.
**Phase**: NORTH_STAR Phase 3 follow-up (gates the install-and-stay-current story for pip users).
**Owner**: TBD — handed off to khimaira-6 via notice.
**Last reviewed**: 2026-05-14
**Estimated effort**: ~1 day.

> **🎯 Goal: make `khimaira sync` do the right thing for users who installed via `pip install khimaira` or `uvx khimaira`, not just for editable-install dev contributors.**

---

## The gap

Today's `khimaira sync` (task #66) is designed for the **editable-install dev workflow** Joseph uses:
- Clone `khimaira` to `~/dev/khimaira`
- `uv sync --all-packages` to get an editable install
- `khimaira sync` does `git pull` + `uv sync` + reapply profile bits

For users who `pip install khimaira` or `uvx khimaira` (the OSS-launch audience), this is broken:
- They don't have `~/dev/khimaira` as a git repo
- `khimaira sync` either no-ops, fails on the git-pull step, or silently re-registers MCP without actually checking for upgrades
- The user's mental model — "I installed khimaira via pip, `khimaira sync` keeps it current" — doesn't match what happens

The OSS install-and-stay-current story doesn't work until this is fixed.

## Two user populations, two definitions of "sync"

| Population | Installed how | What "sync" means |
|---|---|---|
| **Contributors** (Joseph et al.) | `git clone` + `uv sync --all-packages` (editable) | git pull repos, uv sync deps |
| **OSS users** | `uvx khimaira` or `pip install khimaira` (site-packages) | check PyPI for new version, `pip install -U khimaira` (or `uvx --upgrade`), re-apply hooks/MCP if profile changed |

## Design: mode-aware single command (Path A)

`khimaira sync` detects installation mode at runtime and branches behavior:

```python
def detect_install_mode() -> Literal["editable", "site-packages"]:
    """Return 'editable' if khimaira's __file__ is under a workspace
    checkout, 'site-packages' if it's under a venv's site-packages."""
    import khimaira
    path = Path(khimaira.__file__).resolve()
    if "site-packages" in path.parts:
        return "site-packages"
    if (path.parent.parent.parent / "pyproject.toml").exists():
        return "editable"
    return "site-packages"  # default to safer assumption
```

Branch behavior:
- **editable mode** → current behavior (git pull repos + uv sync + reapply profile)
- **site-packages mode** → check PyPI for new version → if newer, `pip install -U khimaira` (or detect uvx and use `uv tool upgrade khimaira`) → reapply profile

Same command, two behaviors, no new CLI surface to document.

## The community profile (related to task #62)

Today's `~/dotfiles/khimaira-profile.yaml` is **Joseph's personal dev profile** — it clones git repos. That's wrong for pip users. Phase 3.3 (task #45) and the task #62 evening block were supposed to ship a **community profile** with a different shape:

```yaml
name: khimaira-community
description: Default profile for pip-installed users.

# NO repos: section — khimaira is already installed via pip/uvx

# Optional dotfiles section — only if user opted in
# dotfiles:
#   repo: <user's own dotfiles>
#   symlinks: [...]

mcp_servers:
  - name: khimaira
    command: uvx khimaira mcp     # ← key difference: pip-installed entry point

supervisor:
  auto_install: true

spa_build: true
install_claude_hooks: true
```

This task assumes the community profile exists (or lands in the same shipment). If task #62 hasn't shipped it yet, this task should include profile creation as a step.

## Implementation outline

### Phase 1 — Mode detection + pip upgrade path (half-day)

1. Add `khimaira.bootstrap.runner.detect_install_mode()`.
2. In `run_sync()`, branch on detected mode:
   - editable: existing path (no change)
   - site-packages: new `check_and_upgrade_pip_package()` helper that:
     - Fetches `https://pypi.org/pypi/khimaira/json`
     - Compares released version vs `khimaira.__version__`
     - If newer: shell out to `pip install --upgrade khimaira` (or `uv tool upgrade khimaira` if installed via `uvx`/`uv tool`)
     - Reports `package-upgrade khimaira  [updated]` row in the standard sync output
3. After upgrade (or no-op), re-run the existing profile-apply steps (symlinks, MCP register, hooks). These work the same in both modes.

### Phase 2 — Profile shape for community users (half-day, may overlap task #62)

1. If `khimaira-profile.community.yaml` doesn't already exist (from task #62), create it.
2. Document the two profile shapes in `docs/PROTOCOL.md`:
   - dev profile: has `repos:` section
   - community profile: no `repos:` section, uses `uvx khimaira mcp` for the MCP command
3. The profile loader should validate: if `repos:` references exist but mode is `site-packages`, surface a warning ("This profile is set up for editable-install; you're pip-installed. Consider switching to the community profile.")

### Phase 3 — Tests + docs

1. Unit tests for `detect_install_mode()` — mock `khimaira.__file__` in both shapes.
2. Unit test for `check_and_upgrade_pip_package` against a mock PyPI response.
3. README install section gets the OSS-user flow:
   ```bash
   uvx khimaira install --profile <community-url>
   # Then later, anytime:
   khimaira sync
   # → checks PyPI, upgrades if needed, reapplies profile
   ```

## Open questions

1. **Detection precision.** Editable installs put the package under `<project>/packages/khimaira/src/khimaira`. PyPI installs put it under `<venv>/lib/python3.X/site-packages/khimaira`. The detection logic above checks for "site-packages" in path parts — robust but worth a unit test against both shapes including the workspace member case.
2. **uvx vs pip detection.** A user might have installed via `uvx khimaira` (uses `uv tool install` under the hood) or via `pip install khimaira` (in a regular venv). The upgrade command differs (`uv tool upgrade khimaira` vs `pip install -U khimaira`). Detect by introspecting the parent of `sys.executable`, or by checking for `uv` on PATH and falling back. Worth getting right.
3. **Auto-upgrade vs prompt.** Should `khimaira sync` auto-run the pip upgrade, or prompt? My recommendation: prompt by default, `--auto-upgrade` flag for cron mode. Consistent with `--auto-restart` deferral for monitor (currently suggestion-only too).
4. **Sibling-package upgrades.** v0.1.0 published `khimaira`, `khimaira-types`, `khimaira-transport`. v0.2.0 will add `khimaira-seance`, `khimaira-specter`, `khimaira-scarlet`, `khimaira-sibyl`. The `pip install -U khimaira` command would only upgrade khimaira itself — does pip transitively upgrade pinned siblings, or do we need to explicitly target each? Probably needs a `pip install -U khimaira khimaira-types khimaira-transport ...` style invocation, or a meta-package.
5. **Hook integration.** Once published `khimaira` is older than what's on PyPI, does the user see a warning at SessionStart? Probably yes — small line: "🔁 khimaira 0.1.0 → 0.2.1 available — run `khimaira sync` to upgrade." Cheap win, but defer to v1.3.

## Validation hooks (how to know it works)

After Phase 1 + 2:
1. On a fresh laptop: `uvx khimaira install --profile <community>` works without git clones.
2. `khimaira sync` checks PyPI, reports current version status.
3. When a new khimaira release lands on PyPI, the laptop's next `khimaira sync` proposes (or auto-runs) the upgrade.
4. After upgrade, MCP + hooks are re-applied automatically — no manual `claude mcp add` needed.

## Connection to other tasks

- **Task #45** (Phase 3.3 — Community profile): this task assumes the community profile exists. If task #45 hasn't landed it, this task may absorb the work or block on it.
- **Task #66** (khimaira sync v1): builds directly on top. v1's mode-detection logic is the new piece.
- **Task #62** (Reframe NORTH_STAR + ship install story): the README install section khimaira-6 is writing should reflect the OSS-user flow that this task enables.

## Anti-scope

- **Not building a package registry of our own.** Read PyPI directly.
- **Not auto-rolling-back on failed upgrade.** If `pip install -U` fails, surface the error and bail. User can manually pin if they need to.
- **Not auto-uninstalling old khimaira-chimera transitional aliases** (if any). Out of scope.
