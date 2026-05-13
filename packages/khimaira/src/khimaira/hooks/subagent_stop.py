#!/usr/bin/env python3
"""khimaira SubagentStop hook — record `~/.claude/agents/khimaira-*` dispatches.

Claude Code's subagent system swaps the model per the agent's frontmatter
(haiku/sonnet/opus). The token savings are real but bypass khimaira's
delegate path — so `khimaira usage savings` can't see them unless we hook
the SubagentStop event and write a UsageRecord ourselves.

Payload contract (empirically captured 2026-05-13 via a probe hook —
docs were misleading about which transcript path to read):
  - session_id: parent session UUID
  - transcript_path: PARENT session's transcript (huge — millions of
    tokens for a long session). NOT what we want.
  - agent_transcript_path: SUBAGENT's transcript JSONL. This is the
    file we parse for the dispatch's usage.
  - cwd, hook_event_name, permission_mode, stop_hook_active
  - agent_id, agent_type (the subagent's `name:` field)
  - subagent_type (alias for agent_type in newer Claude Code versions)
  - last_assistant_message: final text returned by the subagent

The fallback to `transcript_path` exists only as a defensive measure
for older Claude Code versions that may not yet ship the dedicated
`agent_transcript_path` field. On any Claude Code where both are
present, `agent_transcript_path` wins.

Token counts are NOT in the payload — we read them from the subagent
transcript JSONL. Format empirically confirmed against 2026-05-13
sessions:
  - one line per turn
  - line.type == "assistant" → line.message.{model, usage}
  - line.message.usage.{input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens}

Hard rules (match post_tool_use.py):
  - Never block Claude Code. ANY failure → exit 0 silently.
  - Stdlib only. The khimaira package may not be importable from here.
  - Direct filesystem append. No daemon roundtrip, no async.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Mirror khimaira.usage._LOG_FILE so we write to the same JSONL the
# savings command reads. Stdlib-only constraint means we can't import
# khimaira.usage.log_file_path() here.
_LOG_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
)
_LOG_FILE = _LOG_DIR / "usage.jsonl"

# Per-million-token prices in USD. Duplicated from khimaira.usage._PRICES
# to keep this hook stdlib-only. Update both when prices change.
# Match by prefix — full model IDs include date suffixes that vary.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-haiku-4": (0.8, 4.0),
}

# Cache-token multipliers — match khimaira.usage._CACHE_*_MULTIPLIER.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    if not model:
        return 0.0
    matches = [k for k in _PRICES if model.startswith(k)]
    if not matches:
        return 0.0
    key = max(matches, key=len)
    in_per_m, out_per_m = _PRICES[key]
    total = (
        input_tokens * in_per_m
        + output_tokens * out_per_m
        + cache_creation_tokens * in_per_m * _CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * in_per_m * _CACHE_READ_MULTIPLIER
    )
    return total / 1_000_000.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_transcript(transcript_path: Path) -> tuple[str, int, int, int, int]:
    """Sum tokens across all assistant turns in the transcript.

    Returns (model, input_tokens, output_tokens, cache_creation, cache_read).
    Model is the LAST assistant turn's model — multi-turn subagents are
    rare but if they happen, all turns share an agent so the model is
    stable.

    Cache tokens are kept SEPARATE from input_tokens (previously they
    were folded in). Anthropic bills cache_creation at ~1.25x base
    input and cache_read at ~0.1x base input — folding all three into
    input_tokens over-counts the cost. Storing them separately lets
    estimate_cost apply the right multipliers.
    """
    model = ""
    in_tok = 0
    out_tok = 0
    cache_creation = 0
    cache_read = 0
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue
            m = msg.get("model")
            if isinstance(m, str) and m:
                model = m
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            in_tok += int(usage.get("input_tokens") or 0)
            cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            out_tok += int(usage.get("output_tokens") or 0)
    return model, in_tok, out_tok, cache_creation, cache_read


def _provider_for(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gemini-"):
        return "google"
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    return "other"


def _append_record(record: dict) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(data, dict):
        return 0

    agent_type = data.get("subagent_type") or data.get("agent_type") or ""
    if not isinstance(agent_type, str) or not agent_type.startswith("khimaira-"):
        # Not one of ours. Other subagents (Explore, Plan, etc.) are
        # billed against the parent session normally; not our lane.
        return 0

    # Prefer the dedicated subagent-transcript field over the parent's
    # transcript_path. See module docstring for the empirical payload
    # capture that motivated this.
    transcript_path_str = (
        data.get("agent_transcript_path")
        or data.get("subagent_transcript_path")
        or data.get("transcript_path")
        or ""
    )
    if not isinstance(transcript_path_str, str) or not transcript_path_str:
        return 0
    transcript_path = Path(transcript_path_str)
    if not transcript_path.is_file():
        return 0

    try:
        model, in_tok, out_tok, cache_creation, cache_read = _parse_transcript(
            transcript_path
        )
    except OSError:
        return 0

    if not model or (
        in_tok == 0 and out_tok == 0 and cache_creation == 0 and cache_read == 0
    ):
        # Nothing to record. Subagent may have errored before producing
        # any assistant turn.
        return 0

    record = {
        "ts": _now_iso(),
        "task_id": data.get("session_id"),  # ties back to parent session
        "runner": "claude",  # Claude Code is the runner
        "provider": _provider_for(model),
        "model": model,
        "role": agent_type,  # e.g. "khimaira-factual"
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "latency_s": 0.0,  # not exposed by the hook payload
        "estimated_cost_usd": _estimate_cost(
            model,
            in_tok,
            out_tok,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        ),
        "source": "cli",
        "mode": "subagent",
        "escalation_count": 0,
    }

    try:
        _append_record(record)
    except OSError:
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Catch-all — hooks must never bubble exceptions back to Claude Code
        sys.exit(0)
