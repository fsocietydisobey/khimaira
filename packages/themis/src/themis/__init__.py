"""Themis — role-invariant enforcement service for the khimaira workspace.

Exports the public surface used by the daemon API and MCP server:
  - data.py: Invariant, Matcher, EvalResult, ViolationRecord, RuleSet
  - engine.py: evaluate()
  - conditions.py: idle_agents_exist, chat_my_chats_not_called_this_turn
  - violations.py: append_violation, read_violations, compact_if_needed
"""

__version__ = "0.1.0"
