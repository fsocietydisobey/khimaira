"""Compatibility tests for khimaira's legacy Claude hook module path."""

from __future__ import annotations

import json
import subprocess
import sys

from khimaira.hooks import claude_internal_roster_pretool as compatibility_hook
from themis.hooks import claude_internal_roster_pretool as canonical_hook


def test_compatibility_import_reexports_canonical_implementation():
    assert compatibility_hook.evaluate is canonical_hook.evaluate
    assert compatibility_hook.main is canonical_hook.main
    assert compatibility_hook.ROLE_BY_AGENT_TYPE is canonical_hook.ROLE_BY_AGENT_TYPE


def test_legacy_claude_module_execution_preserves_exit_protocol(tmp_path):
    payload = {
        "agent_id": "consultant-compat-123",
        "agent_type": "khimaira-internal-consultant",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/repo/source.py"},
        "cwd": str(tmp_path),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "khimaira.hooks.claude_internal_roster_pretool"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert "IN-CONSULTANT-1" in completed.stderr
