"""LLM usage tracker — record every CLI runner call to a JSONL log.

Migrated from khimaira-legacy. The dev-tool pitch ("khimaira makes your
subscription stretch 5x") requires concrete numbers — this is the
audit trail those numbers come from.

Persists to `~/.local/state/khimaira/usage.jsonl`. Read by:
  - /api/usage (rolling totals — khimaira monitor dashboard)
  - /api/savings (counterfactual — "you'd have spent $X without AMR")
  - check_usage_rate self-watch invariant (rate-anomaly alarm)
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from khimaira_types import UsageRecord

from khimaira.log import get_logger

log = get_logger("usage")

_LOG_DIR = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "khimaira"
_LOG_FILE = _LOG_DIR / "usage.jsonl"

# Per-million-token prices in USD. Best-effort — unknown models record
# token counts but estimate $0. Update when pricing changes.
#
# Match by *prefix*: "claude-opus-4-7-20251022" → "claude-opus-4-7"
# so future minor revs don't need code changes here.
_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic Claude 4.x
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-haiku-4": (0.8, 4.0),
    # Google Gemini 2.5
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.0-pro": (1.25, 10.0),
    "gemini-2.0-flash": (0.075, 0.30),
    # OpenAI Codex (rough; pricing varies across regions/tiers)
    "gpt-5-codex": (3.0, 12.0),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost. Returns 0 for unknown models."""
    if not model:
        return 0.0
    matches = [k for k in _PRICES if model.startswith(k)]
    if not matches:
        return 0.0
    key = max(matches, key=len)
    in_per_m, out_per_m = _PRICES[key]
    return (input_tokens * in_per_m + output_tokens * out_per_m) / 1_000_000.0


def log_file_path() -> Path:
    return _LOG_FILE


@dataclass
class _Recorder:
    """Singleton — append usage records to JSONL, async-safe."""

    _lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def record(
        self,
        *,
        runner: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_s: float,
        role: str | None = None,
        task_id: str | None = None,
        source: str = "cli",
        mode: str = "unknown",
        escalation_count: int = 0,
    ) -> None:
        cost = estimate_cost(model, input_tokens, output_tokens)
        record = UsageRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            task_id=task_id,
            runner=runner,
            provider=provider,  # type: ignore[arg-type]  (Pydantic literal narrowing)
            model=model,
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=latency_s,
            estimated_cost_usd=cost,
            source=source,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            escalation_count=escalation_count,
        )
        try:
            async with self._get_lock():
                await asyncio.to_thread(self._append, record)
        except Exception as exc:
            log.warning("usage: failed to record %s/%s: %s", runner, model, exc)

    @staticmethod
    def _append(record: UsageRecord) -> None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")


_recorder = _Recorder()


def get_recorder() -> _Recorder:
    return _recorder


def runner_to_provider(runner: str) -> str:
    """Map runner name → provider for usage records."""
    return {
        "claude": "anthropic",
        "codex": "openai",
        "gemini": "google",
        "ollama": "local",
        "llm": "other",  # depends on model; "other" is least-wrong default
    }.get(runner, "other")


# ---------------------------------------------------------------------------
# LangChain callback — auto-attached to every model built via config/models.py.
# Migrated from legacy khimaira/usage.py to keep the patterns' usage tracking
# working during the gradual API → CLI substrate migration. Once Phase 10
# (API removal) is complete, this can be deleted.
# ---------------------------------------------------------------------------


def _record_sync(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_s: float,
    role: str | None = None,
    source: str = "langchain",
) -> None:
    """Synchronous append, used by LangChain callback handlers (which run
    in the LLM call's thread, may or may not be the asyncio loop)."""
    import time as _time

    cost = estimate_cost(model, input_tokens, output_tokens)
    record = UsageRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        runner=provider,  # Best mapping when CLI runner unknown — provider name
        provider=provider,  # type: ignore[arg-type]
        model=model,
        role=role,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_s=latency_s,
        estimated_cost_usd=cost,
        source=source,  # type: ignore[arg-type]
    )
    _ = _time  # silence noqa
    try:
        _Recorder._append(record)
    except Exception as exc:
        log.warning("usage: failed to record %s/%s: %s", provider, model, exc)


def make_langchain_callback(provider: str, role: str | None = None):
    """Return a BaseCallbackHandler that records every LangChain LLM call.

    Lazy import — langchain_core is heavy and not all khimaira entry paths
    need it (e.g. `khimaira doctor` shouldn't pay the import cost).
    """
    import time as _time

    from langchain_core.callbacks import BaseCallbackHandler

    class _UsageCallback(BaseCallbackHandler):
        """Captures token usage from LangChain LLM responses.

        Anthropic puts usage under llm_output['usage']; Google under
        llm_output['usage_metadata']. Same shape, different parent key —
        we probe both.
        """

        def on_llm_start(self, *args, **kwargs):
            self._t0 = _time.monotonic()

        def on_llm_end(self, response, **kwargs):
            latency = _time.monotonic() - getattr(self, "_t0", _time.monotonic())
            llm_output = getattr(response, "llm_output", None) or {}

            usage = llm_output.get("usage") or llm_output.get("usage_metadata") or {}
            model = llm_output.get("model_name") or llm_output.get("model") or "unknown"

            input_tokens = (
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("prompt_token_count")
                or 0
            )
            output_tokens = (
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("candidates_token_count")
                or 0
            )

            _record_sync(
                provider=provider,
                model=model,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                latency_s=latency,
                role=role,
                source="langchain",
            )

    return _UsageCallback()
