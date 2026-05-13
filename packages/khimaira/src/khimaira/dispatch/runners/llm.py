"""`llm` runner — Simon Willison's universal CLI.

`llm` is a CLI that wraps ~100+ providers behind one consistent interface:
OpenAI, Anthropic, Gemini, Cohere, Mistral, Ollama (local), OpenRouter,
Together, Anyscale, Groq, Fireworks, and more. Each is loaded as a plugin.

For khimaira, this runner is the "long tail" runner. Devs who want to
experiment with a non-mainstream model (Mistral, DeepSeek, a specific
fine-tune on OpenRouter) install the relevant `llm` plugin and khimaira
can dispatch to it without any code changes here.

Invocation: `llm -m <model_id> "<prompt>"` (stdin alternative also supported).
"""

from __future__ import annotations

import os
import time

from khimaira.log import get_logger

from .base import CLIRunner, RunnerResult, cli_available, run_subprocess

log = get_logger("dispatch.runners.llm")

DEFAULT_CMD = os.environ.get("KHIMAIRA_LLM_CMD", "llm")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_LLM_MODEL", "")  # No default — caller must specify


class LLMRunner:
    """Generic runner backed by Simon Willison's `llm` CLI."""

    name = "llm"

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
                f"`llm` CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_LLM_CMD or install: pip install llm"
            )

        model_id = model or self.default_model
        if not model_id:
            raise ValueError(
                "LLMRunner requires an explicit model — no default. Set "
                "KHIMAIRA_LLM_MODEL or pass model= per call."
            )

        # `llm -m <model> -- <prompt>` is the stable invocation. We pipe
        # the prompt via stdin to avoid argv length limits for big prompts.
        cmd = [self.cmd, "-m", model_id]
        if session_id:
            # `llm` uses --cid (continue id) for resume
            cmd.extend(["--cid", session_id])

        log.info("llm: prompt=%dch, model=%s", len(prompt), model_id)
        t0 = time.monotonic()
        raw = await run_subprocess(cmd, timeout=timeout, cwd=cwd, label="llm", stdin=prompt)
        latency = time.monotonic() - t0

        # `llm` writes plain-text response to stdout by default. `-o usage 1`
        # adds a tail with token counts but isn't universally supported by
        # all plugins; skip for now and report 0 tokens.
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


llm_runner = LLMRunner()


async def run_llm(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> RunnerResult:
    return await llm_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)


_: CLIRunner = llm_runner
