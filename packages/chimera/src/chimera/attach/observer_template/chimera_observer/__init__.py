"""chimera observer — auto-injected into target apps' venvs.

Stdlib-only. Drop into a venv's site-packages and pair with chimera_observer.pth
to auto-load on every Python interpreter start. Registers a global LangChain
callback handler that POSTs hierarchical run events to the chimera daemon.

Wire protocol (chimera-defined, vendor-neutral):
    POST {endpoint}/api/heartbeat
    {
        "project": "...",
        "run_id": "...",
        "parent_run_id": "..." | null,
        "name": "node_name" | "model_name" | None,
        "event": "chain_start" | "chain_end" | "llm_start" | "llm_end" |
                 "tool_start" | "tool_end" | "chain_error" | "llm_error" |
                 "tool_error",
        "ts": <unix_seconds>,
        "extra": {...} | null   # token usage, error msg, etc.
    }

Design rules (load-bearing):
  - Never block the app. POSTs run on a background thread; failure is silent.
  - Never crash the app. Every callback wrapped in try/except.
  - Stdlib only. urllib.request for HTTP. No httpx/requests.
  - Quiet on import failure. If langchain isn't available, attach() no-ops.
  - Cheap on the hot path. Bounded queue (drops events under load) — better
    to lose a heartbeat than degrade the app's latency.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.request
from typing import Any

__version__ = "0.1.0"

_DEFAULT_ENDPOINT = "http://127.0.0.1:8740"
_QUEUE_MAX = 1000  # drop events when queue is full
_POST_TIMEOUT_S = 1.0

_attached = False
_lock = threading.Lock()


def _derive_project() -> str:
    """Project name comes from CHIMERA_PROJECT env, else cwd basename."""
    explicit = os.environ.get("CHIMERA_PROJECT", "").strip()
    if explicit:
        return explicit
    try:
        return os.path.basename(os.getcwd()) or "unknown"
    except OSError:
        return "unknown"


class _ChimeraTracer:
    """Standalone callback handler — built dynamically only when LangChain is
    importable. Defined as a non-class until attach() so we don't pay the
    LangChain import cost when the venv has langchain but the app never uses
    it."""


def _build_tracer():
    """Return a BaseCallbackHandler subclass instance. Done lazily so that
    importing chimera_observer doesn't drag langchain_core into the import
    graph for apps that have it installed but aren't currently running it."""
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:
        return None

    endpoint = os.environ.get("CHIMERA_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")
    project = _derive_project()
    q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
    dropped = [0]

    def _drain() -> None:
        """Background pump. Empties the queue forever, fire-and-forget POSTs."""
        while True:
            try:
                payload = q.get()
            except Exception:
                return
            try:
                data = json.dumps(payload, default=str).encode("utf-8")
                req = urllib.request.Request(
                    endpoint + "/api/heartbeat",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as _:
                    pass
            except Exception:
                # Daemon down, network glitch, anything else — silent.
                # We never want telemetry to break the app.
                pass

    drain_thread = threading.Thread(target=_drain, daemon=True, name="chimera-observer")
    drain_thread.start()

    def _enqueue(event: str, run_id: Any, parent_run_id: Any = None,
                 name: str | None = None, extra: dict | None = None) -> None:
        try:
            q.put_nowait({
                "project": project,
                "run_id": str(run_id) if run_id is not None else None,
                "parent_run_id": str(parent_run_id) if parent_run_id is not None else None,
                "name": name,
                "event": event,
                "ts": time.time(),
                "extra": extra,
            })
        except queue.Full:
            dropped[0] += 1  # surface via stats endpoint someday

    class ChimeraTracer(BaseCallbackHandler):  # type: ignore[misc]
        """LangChain global callback — posts every node/LLM/tool event to chimera."""

        # Chain (node) events
        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                name = (
                    (kwargs.get("name") if isinstance(kwargs, dict) else None)
                    or (serialized.get("name") if isinstance(serialized, dict) else None)
                    or (serialized.get("id", [None])[-1] if isinstance(serialized, dict) and serialized.get("id") else None)
                )
                _enqueue("chain_start", run_id, parent_run_id, name=name)
            except Exception:
                pass

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                _enqueue("chain_end", run_id, parent_run_id)
            except Exception:
                pass

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                _enqueue("chain_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

        # LLM events
        def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                model = None
                if isinstance(serialized, dict):
                    model = (
                        serialized.get("name")
                        or (serialized.get("id", [None])[-1] if serialized.get("id") else None)
                    )
                _enqueue("llm_start", run_id, parent_run_id, name=model)
            except Exception:
                pass

        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                model = None
                if isinstance(serialized, dict):
                    model = (
                        serialized.get("name")
                        or (serialized.get("id", [None])[-1] if serialized.get("id") else None)
                    )
                _enqueue("llm_start", run_id, parent_run_id, name=model)
            except Exception:
                pass

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                extra = {}
                # Token usage extraction — varies by provider
                if response is not None:
                    llm_output = getattr(response, "llm_output", None) or {}
                    if isinstance(llm_output, dict):
                        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
                        if isinstance(usage, dict):
                            extra = {
                                "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                                "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
                            }
                        model = llm_output.get("model_name") or llm_output.get("model")
                        if model:
                            extra["model"] = model
                _enqueue("llm_end", run_id, parent_run_id, extra=extra or None)
            except Exception:
                pass

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                _enqueue("llm_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

        # Tool events
        def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                name = None
                if isinstance(serialized, dict):
                    name = serialized.get("name")
                _enqueue("tool_start", run_id, parent_run_id, name=name)
            except Exception:
                pass

        def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                _enqueue("tool_end", run_id, parent_run_id)
            except Exception:
                pass

        def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
            try:
                _enqueue("tool_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

    return ChimeraTracer()


def attach() -> None:
    """Register the chimera tracer as a global LangChain callback.

    Idempotent. Safe to call multiple times. Silent on every failure mode
    (langchain missing, registration API changed, etc.) — apps must not
    break because of telemetry setup.
    """
    global _attached
    with _lock:
        if _attached:
            return
        _attached = True

    if os.environ.get("CHIMERA_OBSERVER_DISABLE", "").lower() in ("1", "true", "yes"):
        return

    tracer = _build_tracer()
    if tracer is None:
        return  # langchain not in this venv — nothing to do

    # Register globally. LangChain's modern (0.3+) API uses a contextvar-based
    # configure hook. We register and pre-set the contextvar so every
    # CallbackManager.configure() includes our tracer.
    try:
        from contextvars import ContextVar
        from langchain_core.tracers.context import register_configure_hook  # type: ignore[import-not-found]

        # The contextvar holds a list of inheritable handlers
        chimera_handlers_var: ContextVar = ContextVar("chimera_handlers", default=None)
        register_configure_hook(chimera_handlers_var, inheritable=True)
        chimera_handlers_var.set([tracer])
    except Exception:
        # Fallback: try to add to the legacy global callback list, if any
        try:
            import langchain_core.tracers.langchain  # type: ignore  # noqa: F401
            # Older versions had set_global_handler — best-effort
            try:
                from langchain_core.callbacks.manager import set_handler  # type: ignore  # noqa: F401
            except ImportError:
                pass
        except Exception:
            pass
