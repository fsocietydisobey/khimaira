"""Ollama CLI runner — local model dispatch.

The free-tier story. When a dev has Ollama installed alongside their
terminal AI subscription, khimaira routes trivial tasks here and reserves
the subscription for hard tasks. Subscription stretches 3-5x.

Two modes supported:
  - `ollama run <model>`: stdin → stdout, simplest. Used here.
  - HTTP API at localhost:11434: more featureful (streaming, options).
    Could swap in if we need streaming.

Token counts: Ollama's CLI doesn't emit usage stats by default. We can
get them from the HTTP API's `/api/generate` (`prompt_eval_count`,
`eval_count`) — but to keep this runner simple-by-default, we leave
those at 0 here and let the usage tracker note "0-token Ollama record"
which the dashboard can flag.
"""

from __future__ import annotations

import os
import time

from khimaira.log import get_logger

from .base import CLIRunner, RunnerResult, cli_available, run_subprocess

log = get_logger("dispatch.runners.ollama")

DEFAULT_CMD = os.environ.get("KHIMAIRA_OLLAMA_CMD", "ollama")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_OLLAMA_MODEL", "llama3.3:70b")


class OllamaRunner:
    name = "ollama"

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
        session_id: str | None = None,  # noqa: ARG002 — Ollama is stateless per call
        **_: object,
    ) -> RunnerResult:
        if not self.is_available():
            raise RuntimeError(
                f"Ollama CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_OLLAMA_CMD or install: https://ollama.com/download"
            )

        model_id = model or self.default_model

        # `ollama run <model>` reads prompt from stdin in non-interactive use.
        # The CLI auto-detects non-tty stdin; we use --hidethinking to suppress
        # reasoning models' chain-of-thought from polluting the response.
        cmd = [self.cmd, "run", "--hidethinking", model_id]

        log.info("ollama: prompt=%dch, model=%s", len(prompt), model_id)
        t0 = time.monotonic()
        raw = await run_subprocess(cmd, timeout=timeout, cwd=cwd, label="ollama", stdin=prompt)
        latency = time.monotonic() - t0

        # No structured output from `ollama run`. Token counts unavailable
        # without switching to the HTTP API. Return text + 0 tokens; the
        # usage tracker logs latency.
        return RunnerResult(
            text=raw.strip(),
            runner=self.name,
            model=model_id,
            input_tokens=0,
            output_tokens=0,
            latency_s=latency,
            session_id=None,
            raw=raw,
        )


ollama_runner = OllamaRunner()


async def run_ollama(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> RunnerResult:
    return await ollama_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)


_: CLIRunner = ollama_runner
