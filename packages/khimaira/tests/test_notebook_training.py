"""Tests for khimaira.monitor.notebook_training (v2 — resolved notes -> mnemosyne).

The mnemosyne HTTP client (khimaira.hooks.mnemosyne_client.distill) is mocked
throughout — these tests exercise the pair-derivation + fail-open orchestration,
not the real network call.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def training(monkeypatch):
    from khimaira.monitor import notebook_training as training_mod

    importlib.reload(training_mod)
    yield training_mod


def _resolved_note(**overrides) -> dict:
    base = {
        "id": "note-1",
        "title": "Fix the reaper race",
        "raw_text": "raw paste describing the bug",
        "repo": "khimaira",
        "resolution": "Added a lock to serialize access.",
        "pipeline": None,
    }
    base.update(overrides)
    return base


def test_training_domain_uses_note_repo(training):
    assert training.training_domain(_resolved_note(repo="jeevy_portal")) == "jeevy_portal:notes"


def test_training_domain_defaults_to_khimaira(training):
    assert training.training_domain(_resolved_note(repo="")) == "khimaira:notes"


def test_build_training_pair_uses_raw_text_when_no_pipeline(training):
    pair = training.build_training_pair(_resolved_note())
    assert "Fix the reaper race" in pair["instruction"]
    assert "raw paste describing the bug" in pair["instruction"]
    assert pair["response"] == "Added a lock to serialize access."


def test_build_training_pair_prefers_pipeline_summary(training):
    note = _resolved_note(pipeline={"summary": "structured summary"})
    pair = training.build_training_pair(note)
    assert "structured summary" in pair["instruction"]
    assert "raw paste describing the bug" not in pair["instruction"]


def test_promote_resolved_skips_when_no_resolution(training):
    note = _resolved_note(resolution="")
    assert training.promote_resolved(note) is None


def test_promote_resolved_calls_distill_with_derived_pair(training, monkeypatch):
    captured = {}

    def fake_distill(domain, transcript, session_slug, **kwargs):
        captured["domain"] = domain
        captured["transcript"] = transcript
        captured["session_slug"] = session_slug
        return {"ok": True}

    monkeypatch.setattr("khimaira.hooks.mnemosyne_client.distill", fake_distill)

    result = training.promote_resolved(_resolved_note())
    assert result == {"ok": True}
    assert captured["domain"] == "khimaira:notes"
    assert captured["session_slug"] == "notebook-note-1"
    assert "Fix the reaper race" in captured["transcript"]
    assert "Added a lock to serialize access." in captured["transcript"]


def test_promote_resolved_fails_open_when_distill_returns_none(training, monkeypatch):
    monkeypatch.setattr("khimaira.hooks.mnemosyne_client.distill", lambda *a, **kw: None)
    result = training.promote_resolved(_resolved_note())
    assert result is None


def test_promote_resolved_fails_open_on_exception(training, monkeypatch):
    def raising_distill(*a, **kw):
        raise RuntimeError("network exploded")

    monkeypatch.setattr("khimaira.hooks.mnemosyne_client.distill", raising_distill)
    # Must not raise — fail-open is the whole point.
    result = training.promote_resolved(_resolved_note())
    assert result is None


async def test_schedule_promote_runs_in_background(training, monkeypatch):
    called = {}

    def fake_promote(record):
        called["record"] = record
        return {"ok": True}

    monkeypatch.setattr(training, "promote_resolved", fake_promote)

    note = _resolved_note()
    training.schedule_promote(note)
    await asyncio.sleep(0.05)

    assert called["record"] == note
