"""khimaira Claude Code hooks — runnable via `python -m khimaira.hooks.<name>`.

Each module is invoked by Claude Code at the corresponding event:

  - session_start       — boot-time setup (inbox auto-read, handoff
                          surfacing, sister-session discovery)
  - user_prompt_submit  — per-prompt context injection (incoming
                          questions, periodic decision reminder,
                          /rename → khimaira-name sync)
  - post_tool_use       — auto-log file touches after Edit/Write etc.

Settings.json hook commands take the form
`<python> -m khimaira.hooks.<name>`. `install-hooks` derives the python
path from sys.executable at install time and writes the command into
~/.claude/settings.json. This works for both source-checkout installs
and wheel installs (importlib resolves the module either way) — that's
why these scripts live inside the package now, not at workspace root.
"""
