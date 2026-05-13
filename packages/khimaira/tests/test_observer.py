"""Tests for khimaira_observer — the venv-injected LangGraph instrumentation.

Covers:
  - tag_run() ContextVar set/reset
  - _enqueue() stamps correlation_id from the ContextVar
  - _should_skip_external() loop guard (skip khimaira daemon host + loopback)
  - LangSmith bypass shim (no-op when KHIMAIRA_DISABLE_LANGSMITH=true)
  - Auto-correlation: top-level chain_start sets cid; sub-events inherit

The observer uses a module-level singleton (queue + drain thread). Tests
that mutate global state need to reset it between cases or use isolated
imports — handled by the `clean_observer` fixture.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from typing import Any

import pytest


# Make the observer importable. It lives at:
# packages/khimaira/src/khimaira/attach/observer_template/khimaira_observer/
_OBSERVER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "khimaira" / "attach"
    / "observer_template"
)


@pytest.fixture
def clean_observer(monkeypatch: pytest.MonkeyPatch):
    """Reload khimaira_observer with a fresh module state per test.

    The observer module has _attached, _q, _drain_thread, _correlation_id
    singletons that leak between tests if not reset. This fixture
    inserts the observer template path on sys.path, reloads the module,
    and resets module-level state.
    """
    monkeypatch.syspath_prepend(str(_OBSERVER_PATH))
    if "khimaira_observer" in sys.modules:
        del sys.modules["khimaira_observer"]
    import khimaira_observer
    importlib.reload(khimaira_observer)

    # Reset module-level singletons so tests are independent
    khimaira_observer._attached = False
    khimaira_observer._q = None
    khimaira_observer._drain_thread = None
    yield khimaira_observer


def test_tag_run_sets_and_resets_contextvar(clean_observer):
    """tag_run is a context manager — sets cid inside, restores on exit."""
    obs = clean_observer
    assert obs._correlation_id.get() is None

    with obs.tag_run("my-app-run-id"):
        assert obs._correlation_id.get() == "my-app-run-id"

    assert obs._correlation_id.get() is None


def test_tag_run_nested(clean_observer):
    """Nested tag_runs: inner overrides outer for inner scope; outer restores."""
    obs = clean_observer
    with obs.tag_run("outer"):
        assert obs._correlation_id.get() == "outer"
        with obs.tag_run("inner"):
            assert obs._correlation_id.get() == "inner"
        assert obs._correlation_id.get() == "outer"
    assert obs._correlation_id.get() is None


def test_enqueue_stamps_correlation_id_from_var(clean_observer, monkeypatch):
    """_enqueue puts the current correlation_id ContextVar value into the event."""
    obs = clean_observer
    captured: list[dict] = []

    # Replace the singleton init + queue with a list-capture
    def fake_ensure():
        if obs._q is None:
            class FakeQ:
                def put_nowait(self, payload):  # noqa: ARG002
                    captured.append(payload)
            obs._q = FakeQ()  # type: ignore[assignment]

    monkeypatch.setattr(obs, "_ensure_singleton", fake_ensure)

    with obs.tag_run("test-cid-123"):
        obs._enqueue("chain_start", run_id="run-A", name="my_chain")

    assert len(captured) == 1
    assert captured[0]["correlation_id"] == "test-cid-123"
    assert captured[0]["event"] == "chain_start"
    assert captured[0]["name"] == "my_chain"


def test_enqueue_no_cid_when_no_tag_run(clean_observer, monkeypatch):
    """Without tag_run + outside top-level chain, correlation_id is None."""
    obs = clean_observer
    captured: list[dict] = []

    def fake_ensure():
        if obs._q is None:
            class FakeQ:
                def put_nowait(self, payload):
                    captured.append(payload)
            obs._q = FakeQ()  # type: ignore[assignment]

    monkeypatch.setattr(obs, "_ensure_singleton", fake_ensure)

    obs._enqueue("chain_start", run_id="r", name="anon")
    assert captured[0]["correlation_id"] is None


def test_should_skip_external_loopback(clean_observer):
    """Loopback hosts always skipped (no observer-on-loopback recursion)."""
    obs = clean_observer
    obs._endpoint = "http://127.0.0.1:8740"  # set what attach() would set

    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5", "10.0.0.5", ""):
        assert obs._should_skip_external(host) is True, f"should skip {host!r}"


def test_should_skip_external_khimaira_daemon_host(clean_observer):
    """Skip the khimaira daemon's own host even if non-loopback (defensive)."""
    obs = clean_observer
    obs._endpoint = "http://my-khimaira-host.local:8740"

    assert obs._should_skip_external("my-khimaira-host.local") is True


def test_should_skip_external_real_host_not_skipped(clean_observer):
    """Real external hosts are NOT skipped — they get heartbeats."""
    obs = clean_observer
    obs._endpoint = "http://127.0.0.1:8740"

    for host in ("api.openai.com", "serverless.roboflow.com", "httpbin.org"):
        assert obs._should_skip_external(host) is False, f"should NOT skip {host!r}"


def test_emit_external_helpers(clean_observer, monkeypatch):
    """external_start / external_end / external_error helpers stamp correctly."""
    obs = clean_observer
    captured: list[dict] = []

    def fake_ensure():
        if obs._q is None:
            class FakeQ:
                def put_nowait(self, p): captured.append(p)
            obs._q = FakeQ()  # type: ignore[assignment]

    monkeypatch.setattr(obs, "_ensure_singleton", fake_ensure)

    rid = obs._emit_external_start("api.openai.com", "POST", "/v1/messages")
    assert rid is not None
    obs._emit_external_end(rid, "api.openai.com", "POST", 200, 850)

    assert captured[0]["event"] == "external_start"
    assert captured[0]["name"] == "api.openai.com"
    assert captured[0]["extra"]["method"] == "POST"
    assert captured[0]["extra"]["path"] == "/v1/messages"

    assert captured[1]["event"] == "external_end"
    assert captured[1]["extra"]["status"] == 200
    assert captured[1]["extra"]["ms"] == 850
    assert captured[1]["run_id"] == captured[0]["run_id"]  # paired


def test_langsmith_bypass_no_op_when_disabled(clean_observer, monkeypatch):
    """When KHIMAIRA_DISABLE_LANGSMITH is unset, _patch_langsmith_bypass is a no-op."""
    obs = clean_observer
    monkeypatch.delenv("KHIMAIRA_DISABLE_LANGSMITH", raising=False)
    # Should return without raising even if langsmith not installed
    obs._patch_langsmith_bypass()
    # No assertion needed — function returns None and shouldn't raise


def test_langsmith_bypass_skips_when_module_missing(clean_observer, monkeypatch):
    """If langsmith isn't installed, bypass silently no-ops even when env=true."""
    obs = clean_observer
    monkeypatch.setenv("KHIMAIRA_DISABLE_LANGSMITH", "true")

    # Simulate langsmith not installed by hiding it from import
    monkeypatch.setitem(sys.modules, "langsmith", None)
    monkeypatch.setitem(sys.modules, "langsmith.client", None)

    # Should not raise even though env says "bypass"
    obs._patch_langsmith_bypass()


def test_attach_idempotent(clean_observer):
    """attach() can be called multiple times safely."""
    obs = clean_observer
    obs.attach()
    first_attached = obs._attached
    obs.attach()  # second call should be a silent no-op
    assert obs._attached is True
    assert first_attached is True


def test_correlation_id_propagates_across_threads(clean_observer, monkeypatch):
    """ContextVar copies across threads via asyncio.to_thread / threading."""
    import contextvars

    obs = clean_observer
    captured: list[dict] = []

    def fake_ensure():
        if obs._q is None:
            class FakeQ:
                def put_nowait(self, p): captured.append(p)
            obs._q = FakeQ()  # type: ignore[assignment]

    monkeypatch.setattr(obs, "_ensure_singleton", fake_ensure)

    def child():
        obs._enqueue("external_start", run_id="x", name="api.example.com")

    with obs.tag_run("thread-test-cid"):
        # contextvars.copy_context().run mirrors what asyncio.to_thread does
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(child))
        t.start()
        t.join()

    assert len(captured) == 1
    assert captured[0]["correlation_id"] == "thread-test-cid"
