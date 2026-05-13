"""Gemini CLI runner.

Wraps Google's `gemini` CLI. Migrated from khimaira-legacy `cli/runners.py`.

Notes carried over from legacy:
  - `--skip-trust` is required for headless use; without it Gemini refuses
    to run in any directory not previously trusted in interactive mode.
  - `--approval-mode plan` keeps Gemini read-only. Suppresses side-effecting
    tool invocations so they don't leak into the JSON output as agentic
    chatter ("I am now finished with this task" etc.).
  - We deliberately DON'T pass `--include-directories` for cross-project
    scope — gemini-cli appears to eagerly scan/index the listed trees,
    which blocks indefinitely on large repos. The spawned gemini still
    has read access to its `cwd`, which is sufficient.
"""

from __future__ import annotations

import json
import os
import time

from khimaira.log import get_logger

from .base import CLIRunner, RunnerResult, cli_available, run_subprocess

log = get_logger("dispatch.runners.gemini")

DEFAULT_CMD = os.environ.get("KHIMAIRA_GEMINI_CMD", "gemini")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_GEMINI_MODEL", "gemini-2.5-pro")


class GeminiRunner:
    name = "gemini"

    def __init__(self, cmd: str = DEFAULT_CMD, default_model: str = DEFAULT_MODEL) -> None:
        self.cmd = cmd
        self.default_model = default_model

    def is_available(self) -> bool:
        return cli_available(self.cmd)

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        **_: object,
    ) -> RunnerResult:
        if not self.is_available():
            raise RuntimeError(
                f"Gemini CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_GEMINI_CMD or install: npm install -g @google/gemini-cli"
            )

        model_id = model or self.default_model

        cmd = [
            self.cmd,
            "--skip-trust",
            "--approval-mode", "plan",
            "-m", model_id,
            "-p", prompt,
            "-o", "json",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])

        log.info("gemini: prompt=%dch, model=%s, session=%s", len(prompt), model_id, session_id or "new")
        t0 = time.monotonic()
        raw = await run_subprocess(cmd, timeout=timeout, cwd=cwd, label="gemini")
        latency = time.monotonic() - t0

        return self._parse(raw, latency=latency, model_id=model_id)

    def _parse(self, raw: str, *, latency: float, model_id: str) -> RunnerResult:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("gemini: response wasn't JSON (%d chars) — returning raw", len(raw))
            return RunnerResult(text=raw, runner=self.name, model=model_id, latency_s=latency, raw=raw)

        text = data.get("result") or data.get("response") or data.get("text") or ""
        if not text:
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}

        return RunnerResult(
            text=text or raw,
            runner=self.name,
            model=model_id,
            input_tokens=int(
                usage.get("input_tokens")
                or usage.get("prompt_token_count")
                or usage.get("prompt_tokens")
                or 0
            ),
            output_tokens=int(
                usage.get("output_tokens")
                or usage.get("candidates_token_count")
                or usage.get("completion_tokens")
                or 0
            ),
            latency_s=latency,
            session_id=data.get("session_id") or data.get("sessionId"),
            raw=raw,
        )


gemini_runner = GeminiRunner()


async def run_gemini(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> str:
    """Convenience wrapper — returns response text as string for legacy callers."""
    result = await gemini_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)
    return result.text


async def run_gemini_full(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> RunnerResult:
    """Like `run_gemini` but returns the full RunnerResult (with token counts)."""
    return await gemini_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)


_: CLIRunner = gemini_runner
