"""Compatibility shim for :mod:`themis.hooks.claude_internal_roster_pretool`.

Existing ``python -m khimaira.hooks.claude_internal_roster_pretool`` hook
registrations remain valid while implementation lives in standalone Themis.
"""

from themis.hooks.claude_internal_roster_pretool import (
    ROLE_BY_AGENT_TYPE,
    ROSTER_PREFIX,
    _deny,
    _diagnostic,
    _governed_role,
    _run_main_fail_open,
    evaluate,
    main,
)

__all__ = [
    "ROLE_BY_AGENT_TYPE",
    "ROSTER_PREFIX",
    "_deny",
    "_diagnostic",
    "_governed_role",
    "_run_main_fail_open",
    "evaluate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(_run_main_fail_open())
