"""Ownership and portability tests for canonical Themis hook adapters."""

from __future__ import annotations

import ast
import inspect

from themis.hooks import (
    claude_internal_roster_pretool,
    codex_pretool,
    codex_roster_prompts,
    local_themis,
)


def _import_roots(module: object) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_canonical_hooks_never_import_khimaira():
    for module in (
        local_themis,
        codex_pretool,
        claude_internal_roster_pretool,
        codex_roster_prompts,
    ):
        assert "khimaira" not in _import_roots(module)


def test_portable_prompts_use_native_parent_child_reporting_only():
    assert set(codex_roster_prompts.ROLE_TASKS) == {
        "consultant",
        "gatekeeper",
        "agent_1",
        "agent_2",
    }
    prompt_text = "\n".join(codex_roster_prompts.ROLE_TASKS.values()).lower()
    for external_primitive in (
        "khimaira-chat",
        "chat_my_chats",
        "chat_task",
        "session_id",
    ):
        assert external_primitive not in prompt_text
    assert "final" in prompt_text
