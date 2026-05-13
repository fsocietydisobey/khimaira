"""Runtime manager — Pillar 2 of khimaira.

`khimaira dev` orchestrates the project's full dev stack with one command:
detect dev server, spawn it, launch Chrome with --remote-debugging-port for
Specter, probe Postgres, hook into khimaira-monitor for LangGraph runtime.

Single Ctrl-C tears it all down via the tracked process registry from
khimaira/monitor/processes.py — that registry is shared, so daemon-side and
khimaira-dev-side processes coexist without lifecycle confusion.
"""
