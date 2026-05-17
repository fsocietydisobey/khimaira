# Contributing to khimaira

Pre-alpha, active development. Contributions welcome — read this first.

## The short version

- **Bug reports**: open a GitHub issue with a reproduction. The more minimal, the faster the fix.
- **Feature requests**: open an issue describing the use case, not the implementation. We'll discuss before anyone writes code.
- **Architectural changes**: submit an RFC as a GitHub issue first. Phase B v1.9+ has strong design opinions — changes that touch the orchestration stack, chat primitives, or hook pipeline need alignment before a PR.
- **Small fixes** (typos, docs, test coverage): PRs welcome, no issue needed.

## Design principles this project won't compromise on

- `claude/channel` is the transport. We're not abstracting over it or replacing it with polling.
- JSONL over SQLite for session/chat state. Append-only, human-readable, crash-safe.
- Hooks stay thin. SessionStart and UserPromptSubmit inject context — they don't make decisions.
- Role taxonomy (intake → master → agent/observer/architect/critic) is load-bearing. Don't flatten it.

## Development setup

```bash
git clone https://github.com/fsocietydisobey/khimaira.git ~/dev/khimaira
cd ~/dev/khimaira
uv sync --all-packages
uv run pytest packages/khimaira/tests/ -q   # should show 522 passed
uv run khimaira monitor start               # daemon
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for the full setup guide.

## Running tests

```bash
uv run pytest packages/khimaira/tests/ -q        # full suite
uv run pytest packages/khimaira/tests/test_chats.py -q   # chat subsystem only
```

All PRs must pass the full suite. New code should add tests — see `CLAUDE.md` for the test conventions enforced by this project.

## License

MIT. By contributing you agree your changes are licensed under the same terms.
