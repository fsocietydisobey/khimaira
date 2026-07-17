"""Compatibility shim for :mod:`themis.hooks.codex_pretool`.

Existing ``python -m khimaira.hooks.codex_pretool`` registrations remain valid
while policy implementation lives in the standalone Themis package.
"""

from themis.hooks.codex_pretool import (
    _derive_role_from_agent_path,
    _diagnostic,
    _resolve_agent_role,
    _run_main_fail_open,
    evaluate,
    main,
)

__all__ = [
    "_derive_role_from_agent_path",
    "_diagnostic",
    "_resolve_agent_role",
    "_run_main_fail_open",
    "evaluate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(_run_main_fail_open())
