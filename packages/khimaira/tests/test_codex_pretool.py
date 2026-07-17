"""Compatibility tests for khimaira's legacy Codex hook module paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from khimaira.hooks import codex_pretool as compatibility_hook
from khimaira.hooks import codex_roster_prompts as compatibility_prompts
from khimaira.hooks import local_themis as compatibility_local
from themis.hooks import codex_pretool as canonical_hook
from themis.hooks import codex_roster_prompts as canonical_prompts
from themis.hooks import local_themis as canonical_local


def test_compatibility_imports_reexport_canonical_implementations():
    assert compatibility_hook.evaluate is canonical_hook.evaluate
    assert compatibility_hook.main is canonical_hook.main
    assert compatibility_local.evaluate_local is canonical_local.evaluate_local
    assert compatibility_prompts.ROLE_TASKS is canonical_prompts.ROLE_TASKS


def test_compatibility_prompts_are_native_and_preserve_exact_role_names():
    assert set(compatibility_prompts.ROLE_TASKS) == {
        "consultant",
        "gatekeeper",
        "agent_1",
        "agent_2",
    }
    combined = "\n".join(compatibility_prompts.ROLE_TASKS.values()).lower()
    assert "khimaira-chat" not in combined
    assert "chat_task" not in combined
    assert "final" in combined


def test_legacy_codex_module_execution_still_blocks(tmp_path):
    agent_id = "consultant-compat-123"
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "07" / "16"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-test-{agent_id}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"agent_path": "/root/consultant"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = {
        "agent_id": agent_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": "/repo/source.py"},
        "cwd": str(tmp_path),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "khimaira.hooks.codex_pretool"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=5,
    )

    assert completed.returncode == 0
    decision = json.loads(completed.stdout)
    assert decision["decision"] == "block"
    assert "IN-CONSULTANT-1" in decision["reason"]
