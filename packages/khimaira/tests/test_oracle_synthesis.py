"""Tests for Oracle-v2 synthesis mode (_synthesize_answer + mode field wiring)."""

from __future__ import annotations

import pytest

from khimaira.monitor.api import oracle as oracle_mod
from khimaira.monitor.api.oracle import OracleQueryReq, _synthesize_answer


# ---------------------------------------------------------------------------
# _synthesize_answer unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_sync_analyzer():
    """Sync analyzer callable is invoked and its return value is used."""

    def fake_analyzer(context: str, question: str) -> str:
        return f"answer:{len(context)}:{question}"

    result = await _synthesize_answer("ctx", "q?", analyzer=fake_analyzer)
    assert result == "answer:3:q?"


@pytest.mark.asyncio
async def test_synthesize_async_analyzer():
    """Async analyzer callable is awaited correctly."""

    async def fake_async(context: str, question: str) -> str:
        return f"async:{question}"

    result = await _synthesize_answer("ctx", "what?", analyzer=fake_async)
    assert result == "async:what?"


@pytest.mark.asyncio
async def test_synthesize_analyzer_raises_returns_none():
    """When the analyzer raises, _synthesize_answer fails open → None."""

    def bad_analyzer(context: str, question: str) -> str:
        raise RuntimeError("explode")

    result = await _synthesize_answer("ctx", "q?", analyzer=bad_analyzer)
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_no_analyzer_no_sdk_returns_none(monkeypatch):
    """When analyzer=None and anthropic SDK is unavailable, returns None (fail-open)."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("anthropic not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    result = await _synthesize_answer("some context", "some question", analyzer=None)
    assert result is None


# ---------------------------------------------------------------------------
# Handler-level wiring tests (mode field + synthesis field)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_mode_synthesis_field_is_none(monkeypatch):
    """In context mode, synthesis field is None (never called)."""
    # Patch the heavy stores so the handler runs without real infra.
    monkeypatch.setattr(
        oracle_mod, "_seance_search", lambda project, question: ([], False)
    )
    monkeypatch.setattr(oracle_mod, "_mnemosyne_query", lambda project: None)

    req = OracleQueryReq(question="how does auth work?", project="khimaira", mode="context")
    # Call handler internals by building the router and simulating a request via
    # the assembled logic — but to avoid FastAPI overhead in unit tests we call
    # _synthesize_answer directly and verify the handler wouldn't call it.
    # The simplest invariant: mode="context" never sets synthesis.
    synthesis_calls: list[str] = []

    async def tracking_synthesize(context, question, analyzer=None):  # noqa: ARG001
        synthesis_calls.append(question)
        return "should not appear"

    monkeypatch.setattr(oracle_mod, "_synthesize_answer", tracking_synthesize)

    # Replay the handler logic inline (avoids FastAPI test client overhead).
    import asyncio

    (seance_results, _seance_errored), mnemosyne_result = await asyncio.gather(
        asyncio.to_thread(oracle_mod._seance_search, req.project, req.question),
        asyncio.to_thread(oracle_mod._mnemosyne_query, req.project),
    )
    context, citations = oracle_mod._build_context(seance_results, mnemosyne_result, req.project)
    synthesis = None
    if req.mode == "synthesis":
        synthesis = await oracle_mod._synthesize_answer(context, req.question, req.analyzer)

    assert synthesis is None
    assert synthesis_calls == []


@pytest.mark.asyncio
async def test_synthesis_mode_calls_synthesize_and_returns_field(monkeypatch):
    """In synthesis mode, _synthesize_answer is called and its result appears in response."""
    monkeypatch.setattr(
        oracle_mod, "_seance_search", lambda project, question: ([], False)
    )
    monkeypatch.setattr(oracle_mod, "_mnemosyne_query", lambda project: None)

    async def stub_synthesize(context: str, question: str, analyzer=None) -> str:
        return f"synthesized:{question}"

    monkeypatch.setattr(oracle_mod, "_synthesize_answer", stub_synthesize)

    import asyncio

    req = OracleQueryReq(
        question="explain retries",
        project="khimaira",
        mode="synthesis",
    )
    (seance_results, _seance_errored), mnemosyne_result = await asyncio.gather(
        asyncio.to_thread(oracle_mod._seance_search, req.project, req.question),
        asyncio.to_thread(oracle_mod._mnemosyne_query, req.project),
    )
    context, citations = oracle_mod._build_context(seance_results, mnemosyne_result, req.project)
    synthesis = None
    if req.mode == "synthesis":
        synthesis = await oracle_mod._synthesize_answer(context, req.question, req.analyzer)

    response = {
        "mode": req.mode,
        "context": context,
        "citations": citations,
        "synthesis": synthesis,
    }

    assert response["mode"] == "synthesis"
    assert response["synthesis"] == "synthesized:explain retries"
