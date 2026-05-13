"""OpenAI Codex CLI runner.

Wraps the `codex` binary. New for khimaira v2 — legacy didn't have a Codex
runner because the previous architecture was Claude+Gemini-centric. Now
that we're shell-agnostic (Phase 0 vision), Codex is a first-class peer.

Codex CLI's invocation is roughly: `codex exec "<prompt>" -m <model>`. The
exact flag set depends on Codex CLI version; this file targets the
2026-current shape and is the right place to update if OpenAI ships a
breaking change.
"""

from __future__ import annotations

import json
import os
import time

from khimaira.log import get_logger

from .base import CLIRunner, RunnerResult, cli_available, run_subprocess

log = get_logger("dispatch.runners.codex")

DEFAULT_CMD = os.environ.get("KHIMAIRA_CODEX_CMD", "codex")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_CODEX_MODEL", "gpt-5-codex")


class CodexRunner:
    """CLIRunner implementation for OpenAI Codex CLI."""

    name = "codex"

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
                f"Codex CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_CODEX_CMD or install: npm install -g @openai/codex"
            )

        model_id = model or self.default_model

        # Codex's `exec` subcommand runs a one-shot prompt. The `--json` flag
        # gives us structured output with token usage when available.
        cmd = [
            self.cmd, "exec",
            "--model", model_id,
            "--json",
            prompt,
        ]
        if session_id:
            cmd.extend(["--session", session_id])

        log.info("codex: prompt=%dch, model=%s", len(prompt), model_id)
        t0 = time.monotonic()
        raw = await run_subprocess(cmd, timeout=timeout, cwd=cwd, label="codex")
        latency = time.monotonic() - t0

        return self._parse(raw, latency=latency, model_id=model_id)

    def _parse(self, raw: str, *, latency: float, model_id: str) -> RunnerResult:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("codex: response wasn't JSON (%d chars) — returning raw", len(raw))
            return RunnerResult(text=raw, runner=self.name, model=model_id, latency_s=latency, raw=raw)

        # Codex CLI shape (2026-current): { "output": "...", "usage": {"prompt_tokens": ..., "completion_tokens": ...} }
        # Fall back to other plausible field names since this CLI is fast-moving.
        text = (
            data.get("output")
            or data.get("text")
            or data.get("result")
            or data.get("response")
            or ""
        )
        if not text and isinstance(data.get("messages"), list):
            for m in data["messages"]:
                if isinstance(m, dict) and m.get("role") == "assistant":
                    text += m.get("content", "")

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}

        return RunnerResult(
            text=text or raw,
            runner=self.name,
            model=model_id,
            input_tokens=int(
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or 0
            ),
            output_tokens=int(
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or 0
            ),
            latency_s=latency,
            session_id=data.get("session_id") or data.get("sessionId"),
            raw=raw,
        )


codex_runner = CodexRunner()


async def run_codex(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> RunnerResult:
    return await codex_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)


_: CLIRunner = codex_runner
