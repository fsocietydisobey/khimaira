"""Claude Code CLI runner.

Wraps the `claude` binary (Claude Code) with khimaira's session-resume,
extended-thinking, and permission-mode conventions. Migrated from
khimaira-legacy `cli/runners.py`; cleaned up to drop the global session
state in favor of caller-passed session IDs.
"""

from __future__ import annotations

import json
import os
import time

from khimaira.log import get_logger

from .base import CLIRunner, RunnerResult, cli_available, run_subprocess

log = get_logger("dispatch.runners.claude")

# Defaults — overridable via env or per-call kwargs.
DEFAULT_CMD = os.environ.get("KHIMAIRA_CLAUDE_CMD", "claude")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_CLAUDE_MODEL", "claude-opus-4-7")


class ClaudeAuthError(RuntimeError):
    """Claude CLI signaled a hard auth/billing failure — credits exhausted,
    invalid key, rate limit. The classifier MUST NOT retry on these; doing
    so just burns more quota for the same answer.
    """


# Substrings that mean "stop calling Claude until the user fixes something."
# Matched against the JSON `result` field of error responses.
_HARD_STOP_PATTERNS = (
    "credit balance is too low",
    "credit balance too low",
    "billing",
    "invalid api key",
    "authentication",
    "rate limit",
    "rate_limit",
)


class ClaudeRunner:
    """CLIRunner implementation for Claude Code."""

    name = "claude"

    def __init__(
        self,
        cmd: str = DEFAULT_CMD,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
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
        # Claude-specific kwargs (others ignored)
        effort: str = "medium",
        permission_mode: str = "acceptEdits",
        extra_dirs: list[str] | None = None,
        **_: object,
    ) -> RunnerResult:
        if not self.is_available():
            raise RuntimeError(
                f"Claude CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_CLAUDE_CMD or install Claude Code."
            )

        model_id = model or self.default_model

        # --strict-mcp-config + empty --mcp-config prevents the spawned
        # claude from loading khimaira's own MCP entry — which would spawn
        # a child khimaira that grabs the pidlock and kills its parent.
        # Less aggressive than --bare; preserves file-write permissions
        # and CLAUDE.md auto-discovery, which architect/implement need.
        cmd = [
            self.cmd,
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--model", model_id,
            "--effort", effort,
            "-p", prompt,
            "--output-format", "json",
        ]
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
        for extra in (extra_dirs or []):
            cmd.extend(["--add-dir", extra])
        if session_id:
            cmd.extend(["--resume", session_id])

        log.info("claude: prompt=%dch, model=%s, session=%s", len(prompt), model_id, session_id or "new")
        t0 = time.monotonic()
        raw = await run_subprocess(cmd, timeout=timeout, cwd=cwd, label="claude")
        latency = time.monotonic() - t0

        return self._parse(raw, latency=latency, model_id=model_id)

    def _parse(self, raw: str, *, latency: float, model_id: str) -> RunnerResult:
        """Extract text + token counts + session id from claude's --output-format json.

        Detects hard-stop conditions (credit-low, auth, rate-limit) and raises
        ClaudeAuthError so callers (notably run_structured) skip retries. Without
        this, a credits-low classifier call retries 3× and burns whatever's left.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("claude: response wasn't JSON (%d chars) — returning raw", len(raw))
            return RunnerResult(
                text=raw, runner=self.name, model=model_id, latency_s=latency, raw=raw,
            )

        # Hard-stop detection: claude emits is_error=true for billing/auth/rate-limit.
        # Don't retry these — the user has to fix something at the account level.
        if data.get("is_error"):
            err_text = data.get("result", "") or ""
            err_lower = err_text.lower()
            if any(p in err_lower for p in _HARD_STOP_PATTERNS):
                log.error(
                    "claude: HARD STOP — %s (api_status=%s). Refusing further calls "
                    "in this dispatch.",
                    err_text, data.get("api_error_status"),
                )
                raise ClaudeAuthError(
                    f"Claude refused — {err_text}. "
                    "Add credits / fix auth / wait out rate limit before retrying."
                )

        # Text — Claude Code emits `result` (top-level) or content blocks
        result = data.get("result", "") or ""
        if not result:
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    result += block.get("text", "")

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        session_id = data.get("session_id") or data.get("sessionId")

        return RunnerResult(
            text=result or raw,
            runner=self.name,
            model=model_id,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            latency_s=latency,
            session_id=session_id,
            raw=raw,
        )


# Module-level singleton for convenience
claude_runner = ClaudeRunner()


async def run_claude(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> str:
    """Convenience wrapper around `claude_runner.run()` — returns the response
    text as a string.

    Backwards compatibility: legacy khimaira callers (migrated nodes,
    `mcp__khimaira__research`, `mcp__khimaira__architect`) expect string-return
    semantics. New code should use `claude_runner.run()` directly to get the
    full `RunnerResult` (with token counts, latency, session id).
    """
    result = await claude_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)
    return result.text


async def run_claude_full(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    **kwargs: object,
) -> RunnerResult:
    """Like `run_claude` but returns the full RunnerResult. Used by khimaira
    task / dispatch / structured-output flows that need token counts."""
    return await claude_runner.run(prompt, model=model, timeout=timeout, cwd=cwd, **kwargs)


# Type hint that this implements the protocol — module-level assertion-style
_: CLIRunner = claude_runner
