"""Claude Code CLI runner.

Wraps the `claude` binary (Claude Code) with khimaira's session-resume,
extended-thinking, and permission-mode conventions. Migrated from
khimaira-legacy `cli/runners.py`; cleaned up to drop the global session
state in favor of caller-passed session IDs.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from khimaira.log import get_logger

from .base import (
    CLIRunner,
    RunnerResult,
    _build_subprocess_env,
    cli_available,
    run_subprocess,
)

log = get_logger("dispatch.runners.claude")

# Defaults — overridable via env or per-call kwargs.
DEFAULT_CMD = os.environ.get("KHIMAIRA_CLAUDE_CMD", "claude")
DEFAULT_MODEL = os.environ.get("KHIMAIRA_CLAUDE_MODEL", "claude-opus-4-7")


class ClaudeAuthError(RuntimeError):
    """Hard account-level failure — credits exhausted, invalid key, billing,
    account usage limit. MUST NOT retry — the user has to fix something at
    the account level before Claude can be called again.
    """


class ClaudeTransientError(RuntimeError):
    """Server-side transient overload — Anthropic is temporarily rate-limiting
    capacity (not the account's usage quota). Safe to retry with exponential
    backoff after Claude Code exhausts its own ~10 internal retries.

    Distinct from ClaudeAuthError: the error is infrastructure-side and WILL
    clear without user action; retrying is correct.
    """


# Account-level hard stops — user must fix before retrying.
# Matched against the JSON `result` field (case-insensitive substring).
_ACCOUNT_HARD_STOP_PATTERNS = (
    "credit balance is too low",
    "credit balance too low",
    "billing",
    "invalid api key",
    "authentication",
    "usage limit",  # account usage cap, not server overload
)

# Server-side transient overload — retriable with backoff.
# Joseph's exact post-exhaustion string: "Server is temporarily limiting
# requests (not your usage limit)". Include both specific and generic
# overloaded_error patterns; do NOT include generic "rate limit" (it's
# the conflation source that made all rate-limits hard-stop previously).
_TRANSIENT_OVERLOAD_PATTERNS = (
    "server is temporarily limiting requests",
    "not your usage limit",
    "overloaded_error",
    "overloaded",
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

        # Error classification: three-way per #13a analysis.
        #   1. Account-level hard stop (billing/auth/usage-cap) → ClaudeAuthError.
        #      Do NOT retry — user must act.
        #   2. Server transient overload → ClaudeTransientError.
        #      Retriable with backoff after CC's own ~10 internal retries exhaust.
        #   3. Other errors fall through to normal result handling.
        if data.get("is_error"):
            err_text = data.get("result", "") or ""
            err_lower = err_text.lower()
            if any(p in err_lower for p in _ACCOUNT_HARD_STOP_PATTERNS):
                log.error(
                    "claude: HARD STOP (account) — %s (api_status=%s). "
                    "User must fix credits/auth before retrying.",
                    err_text, data.get("api_error_status"),
                )
                raise ClaudeAuthError(
                    f"Claude refused (account-level) — {err_text}. "
                    "Add credits / fix auth before retrying."
                )
            if any(p in err_lower for p in _TRANSIENT_OVERLOAD_PATTERNS):
                log.warning(
                    "claude: TRANSIENT OVERLOAD — %s (api_status=%s). "
                    "Retriable with backoff.",
                    err_text, data.get("api_error_status"),
                )
                raise ClaudeTransientError(
                    f"Claude overloaded (transient) — {err_text}. "
                    "Retry with exponential backoff."
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

    async def stream(
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
    ):
        """Stream the response as a sequence of StreamChunks.

        Uses `claude --output-format stream-json --include-partial-messages`
        and parses Anthropic's Messages API stream events
        newline-delimited from stdout. Yields one StreamChunk per
        `content_block_delta` event (text deltas as they arrive) and
        one FINAL chunk on the `result` event with full usage + model
        + session_id metadata.

        Empirical event format (verified 2026-05-13):
          - system / hook_started / hook_response / init / status — skipped
          - stream_event.event.message_start — contains initial model
          - stream_event.event.content_block_delta with delta.type=text_delta
            → yields one chunk per delta with delta.text
          - stream_event.event.message_stop — end marker
          - assistant — intermediate snapshot, skipped
          - result — final event with total cost, full result text, usage

        Falls back gracefully if any event line doesn't match the
        expected shape — bad lines are skipped, the stream continues.
        """
        from .base import StreamChunk

        if not self.is_available():
            raise RuntimeError(
                f"Claude CLI not found at `{self.cmd}`. "
                "Set KHIMAIRA_CLAUDE_CMD or install Claude Code."
            )

        model_id = model or self.default_model

        cmd = [
            self.cmd,
            "--strict-mcp-config",
            "--mcp-config",
            "/dev/null",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode",
            permission_mode,
            "--model",
            model_id,
        ]
        if effort and effort != "medium":
            cmd.extend(["--effort", effort])
        for d in extra_dirs or []:
            cmd.extend(["--add-dir", d])
        if session_id:
            cmd.extend(["--session-id", session_id])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_subprocess_env(),
            cwd=cwd,
        )
        # Feed prompt via stdin (matches the existing run() pattern).
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        # Track running usage so the final chunk has the right totals.
        running_model = model_id
        running_input = 0
        running_output = 0
        running_cache_creation = 0
        running_cache_read = 0
        running_session_id = session_id
        final_text = ""

        assert proc.stdout is not None
        try:
            while True:
                if timeout is not None:
                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.communicate()
                        raise TimeoutError(
                            f"claude streaming timed out after {timeout}s"
                        ) from None
                else:
                    line = await proc.stdout.readline()
                if not line:
                    break

                raw_line = line.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue

                rec_type = rec.get("type")

                if rec_type == "stream_event":
                    ev = rec.get("event") or {}
                    if not isinstance(ev, dict):
                        continue
                    ev_type = ev.get("type")
                    if ev_type == "message_start":
                        msg = ev.get("message") or {}
                        if isinstance(msg, dict):
                            m = msg.get("model")
                            if isinstance(m, str) and m:
                                running_model = m
                            sid = rec.get("session_id")
                            if isinstance(sid, str) and sid:
                                running_session_id = sid
                    elif ev_type == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if (
                            isinstance(delta, dict)
                            and delta.get("type") == "text_delta"
                        ):
                            txt = delta.get("text") or ""
                            if txt:
                                final_text += txt
                                yield StreamChunk(text=txt, is_final=False)
                    # content_block_start / message_delta / message_stop:
                    # no text to surface; ignore.
                elif rec_type == "result":
                    # Final summary. Pull totals from the rich usage field.
                    if rec.get("is_error"):
                        # Surface error in the final chunk's text so callers
                        # see something — non-fatal: stream still completes.
                        err_msg = str(rec.get("result", "") or "claude error")
                        yield StreamChunk(
                            text=f"\n[claude error: {err_msg}]",
                            is_final=False,
                        )
                    else:
                        # If we somehow saw no deltas (rare), pick up the
                        # full text from result.
                        if not final_text:
                            r = rec.get("result")
                            if isinstance(r, str):
                                final_text = r
                                yield StreamChunk(text=r, is_final=False)
                    usage = rec.get("usage") or {}
                    if isinstance(usage, dict):
                        running_input = int(usage.get("input_tokens") or 0)
                        running_output = int(usage.get("output_tokens") or 0)
                        running_cache_creation = int(
                            usage.get("cache_creation_input_tokens") or 0
                        )
                        running_cache_read = int(
                            usage.get("cache_read_input_tokens") or 0
                        )
                    sid = rec.get("session_id")
                    if isinstance(sid, str) and sid:
                        running_session_id = sid
                # system / assistant / etc — skip silently
        finally:
            # Drain stderr so we don't leak a half-closed pipe.
            try:
                await proc.communicate()
            except Exception:
                pass

        # Final chunk — single source of truth for usage + model.
        # cache_creation + cache_read folded into input_tokens here so the
        # StreamChunk's shape matches RunnerResult (the non-streaming
        # path does the same fold). Callers that care about the
        # breakdown should record them as separate fields on UsageRecord
        # (the SubagentStop hook is the canonical example).
        yield StreamChunk(
            text="",
            is_final=True,
            model=running_model,
            input_tokens=running_input + running_cache_creation + running_cache_read,
            output_tokens=running_output,
            session_id=running_session_id,
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
