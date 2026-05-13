"""CLI runner registry — the only place khimaira talks to LLMs.

Pure subprocess substrate. No API SDK calls anywhere else in khimaira.

Public surface:
  - `RUNNERS`: dict mapping runner name → CLIRunner instance
  - `get_runner(name)`: lookup with sensible error
  - `available_runners()`: probe `is_available()` on each, return what's installed
  - `run_<name>(...)` convenience wrappers per runner
"""

from __future__ import annotations

from .base import (
    CLIRunner,
    RunnerResult,
    cli_available,
    kill_all_subprocesses,
    run_subprocess,
)
from .claude import ClaudeRunner, claude_runner, run_claude
from .codex import CodexRunner, codex_runner, run_codex
from .gemini import GeminiRunner, gemini_runner, run_gemini
from .llm import LLMRunner, llm_runner, run_llm
from .ollama import OllamaRunner, ollama_runner, run_ollama

# Single source of truth — register every runner here. The router walks this
# dict; the auto-classifier's recommendation must produce a name from this
# keyset.
RUNNERS: dict[str, CLIRunner] = {
    "claude": claude_runner,
    "codex": codex_runner,
    "gemini": gemini_runner,
    "ollama": ollama_runner,
    "llm": llm_runner,
}


def get_runner(name: str) -> CLIRunner:
    """Look up a runner by name. Raises KeyError with a helpful message."""
    runner = RUNNERS.get(name)
    if runner is None:
        raise KeyError(
            f"Unknown runner: {name!r}. Known: {sorted(RUNNERS)}. "
            "Add new runners by registering in khimaira/dispatch/runners/__init__.py."
        )
    return runner


def available_runners() -> dict[str, bool]:
    """Probe each runner. Returns {name: True/False}.

    Cheap — `is_available()` is just a `which` lookup. Used by `khimaira doctor`
    and the dashboard's available-runners panel.
    """
    return {name: runner.is_available() for name, runner in RUNNERS.items()}


__all__ = [
    "CLIRunner",
    "RunnerResult",
    "RUNNERS",
    "get_runner",
    "available_runners",
    "cli_available",
    "kill_all_subprocesses",
    "run_subprocess",
    # convenience callables
    "run_claude",
    "run_codex",
    "run_gemini",
    "run_ollama",
    "run_llm",
    # runner classes (for testing / advanced override)
    "ClaudeRunner",
    "CodexRunner",
    "GeminiRunner",
    "OllamaRunner",
    "LLMRunner",
    "claude_runner",
    "codex_runner",
    "gemini_runner",
    "ollama_runner",
    "llm_runner",
]
