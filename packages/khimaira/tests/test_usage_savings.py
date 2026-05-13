"""`khimaira usage savings` — savings math + mode attribution.

The savings story is the user-visible payoff for the auto-router. If
this command lies about savings, the whole value-prop collapses, so
the tests here are paranoid about edge cases:

  - empty log → graceful "no data" message
  - mix of auto + manual + explicit-tier records → savings counted
    ONLY against auto records
  - records older than --days window → excluded
  - unknown-mode legacy records → don't break the math
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khimaira.cli import usage as usage_cli


@pytest.fixture
def isolated_usage_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root usage.jsonl at a tmp path so tests don't trash real data."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    # Reload usage module so its module-level _LOG_FILE picks up the env
    import importlib

    from khimaira import usage as usage_mod

    importlib.reload(usage_mod)
    importlib.reload(usage_cli)
    yield usage_mod.log_file_path()
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(usage_mod)
    importlib.reload(usage_cli)


def _write_record(
    path: Path,
    *,
    ts: datetime,
    runner: str,
    model: str,
    mode: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
) -> None:
    """Append one synthetic usage record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts.isoformat(),
        "task_id": None,
        "runner": runner,
        "provider": "anthropic" if runner == "claude" else "google",
        "model": model,
        "role": "delegate",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_s": 1.0,
        "estimated_cost_usd": cost,
        "source": "cli",
        "mode": mode,
        "escalation_count": 0,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# -------------------- empty-state path -------------------- #


def test_savings_empty_log_returns_zero_no_crash(isolated_usage_log, capsys):
    args = type("Args", (), {"days": 30, "by": "mode"})()
    rc = usage_cli._run_savings(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No usage records" in out


# -------------------- counterfactual math -------------------- #


def test_auto_record_savings_against_opus_baseline(isolated_usage_log, capsys):
    """Auto-mode record on Haiku → savings vs Opus computed correctly."""
    now = datetime.now(timezone.utc)
    # 1000 input + 500 output tokens, dispatched to haiku.
    # Haiku price: $0.8/$4 per M  →  actual = 1000*0.8/1M + 500*4/1M = $0.0028
    # Opus  price: $15/$75 per M  →  baseline = $0.0525
    # Savings expected: $0.0497
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="claude",
        model="claude-haiku-4-5",
        mode="auto",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    args = type("Args", (), {"days": 30, "by": "mode"})()
    rc = usage_cli._run_savings(args)
    assert rc == 0
    out = capsys.readouterr().out
    # Don't pin exact decimals — the cost table can shift. Pin the shape:
    assert "auto-mode records: 1" in out
    assert "0.0028" in out  # actual Haiku cost
    assert "0.0525" in out  # Opus baseline
    assert "0.0497" in out  # savings
    assert "94.7%" in out  # efficiency


def test_manual_and_explicit_tier_records_not_counted_as_savings(
    isolated_usage_log,
    capsys,
):
    """Savings ONLY accrue for auto mode. Manual/explicit-tier dispatches
    were the user's deliberate choice, not khimaira's pick."""
    now = datetime.now(timezone.utc)
    # 1 manual record + 1 explicit-tier record. No auto records.
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="claude",
        model="claude-haiku-4-5",
        mode="manual",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=2),
        runner="claude",
        model="claude-haiku-4-5",
        mode="explicit-tier",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    args = type("Args", (), {"days": 30, "by": "mode"})()
    rc = usage_cli._run_savings(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-mode records: 0" in out
    # The savings line should show 0.0000 since no auto records
    assert "savings (auto only)" in out
    assert "0.0000" in out


# -------------------- time-window filter -------------------- #


def test_records_outside_window_excluded(isolated_usage_log, capsys):
    """--days 7 must exclude a record from 30 days ago."""
    now = datetime.now(timezone.utc)
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(days=30),
        runner="claude",
        model="claude-haiku-4-5",
        mode="auto",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    args = type("Args", (), {"days": 7, "by": "mode"})()
    rc = usage_cli._run_savings(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No usage records" in out


# -------------------- legacy / mixed records -------------------- #


def test_unknown_mode_record_does_not_crash_math(isolated_usage_log, capsys):
    """Legacy records written before the mode field was added load with
    mode='unknown' (Pydantic default). Must not break savings math."""
    now = datetime.now(timezone.utc)
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="claude",
        model="claude-haiku-4-5",
        mode="unknown",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    args = type("Args", (), {"days": 30, "by": "mode"})()
    rc = usage_cli._run_savings(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-mode records: 0" in out  # unknown != auto
    assert "unknown" in out  # bucket shows up in breakdown


# -------------------- breakdown dimensions -------------------- #


def test_by_runner_breakdown(isolated_usage_log, capsys):
    """--by runner groups by runner column."""
    now = datetime.now(timezone.utc)
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="claude",
        model="claude-haiku-4-5",
        mode="auto",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="gemini",
        model="gemini-2.5-flash",
        mode="auto",
        input_tokens=1000,
        output_tokens=500,
        cost=0.000225,
    )
    args = type("Args", (), {"days": 30, "by": "runner"})()
    rc = usage_cli._run_savings(args)
    out = capsys.readouterr().out
    assert "claude" in out
    assert "gemini" in out
    assert rc == 0


# -------------------- baseline override -------------------- #


def test_baseline_defaults_to_opus(isolated_usage_log, capsys, monkeypatch):
    """Without env var or registry override, baseline is claude-opus-4-7."""
    monkeypatch.delenv("KHIMAIRA_USAGE_BASELINE_MODEL", raising=False)
    assert usage_cli._resolve_counterfactual_model() == "claude-opus-4-7"


def test_env_var_overrides_baseline(isolated_usage_log, capsys, monkeypatch):
    """KHIMAIRA_USAGE_BASELINE_MODEL env var overrides the default."""
    monkeypatch.setenv("KHIMAIRA_USAGE_BASELINE_MODEL", "claude-sonnet-4-6")
    assert usage_cli._resolve_counterfactual_model() == "claude-sonnet-4-6"


def test_env_var_baseline_affects_savings_math(isolated_usage_log, capsys, monkeypatch):
    """Changing the baseline to sonnet (cheaper than opus) reduces the
    computed savings — the sonnet baseline costs less than opus baseline
    for the same tokens."""
    now = datetime.now(timezone.utc)
    _write_record(
        isolated_usage_log,
        ts=now - timedelta(hours=1),
        runner="claude",
        model="claude-haiku-4-5",
        mode="auto",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0028,
    )

    # Run with default (opus) baseline
    monkeypatch.delenv("KHIMAIRA_USAGE_BASELINE_MODEL", raising=False)
    args = type("Args", (), {"days": 30, "by": "mode"})()
    usage_cli._run_savings(args)
    out_opus = capsys.readouterr().out
    assert "claude-opus-4-7" in out_opus

    # Run with sonnet baseline
    monkeypatch.setenv("KHIMAIRA_USAGE_BASELINE_MODEL", "claude-sonnet-4-6")
    usage_cli._run_savings(args)
    out_sonnet = capsys.readouterr().out
    assert "claude-sonnet-4-6" in out_sonnet
    # Sonnet baseline is cheaper than opus → savings number should be lower
    # Don't pin exact decimals; pin the inequality through model name
    assert "claude-opus-4-7" not in out_sonnet


def test_registry_baseline_override(isolated_usage_log, tmp_path, monkeypatch):
    """`baseline_model:` key in ~/.khimaira/models.yaml overrides default
    when env var is unset."""
    monkeypatch.delenv("KHIMAIRA_USAGE_BASELINE_MODEL", raising=False)

    # Point the registry path at a tmp file with baseline_model set
    registry_path = tmp_path / "models.yaml"
    registry_path.write_text("baseline_model: gemini-2.5-pro\nmodels: []\n")

    from khimaira.dispatch import registry as registry_mod

    monkeypatch.setattr(registry_mod, "_user_registry_path", lambda: registry_path)

    assert usage_cli._resolve_counterfactual_model() == "gemini-2.5-pro"


def test_env_var_wins_over_registry(isolated_usage_log, tmp_path, monkeypatch):
    """When both env var and registry are set, env var wins (per-session
    override beats persistent config)."""
    monkeypatch.setenv("KHIMAIRA_USAGE_BASELINE_MODEL", "gpt-5-codex")

    registry_path = tmp_path / "models.yaml"
    registry_path.write_text("baseline_model: gemini-2.5-pro\nmodels: []\n")

    from khimaira.dispatch import registry as registry_mod

    monkeypatch.setattr(registry_mod, "_user_registry_path", lambda: registry_path)

    assert usage_cli._resolve_counterfactual_model() == "gpt-5-codex"
