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

Registration approach:
    LangChain's `register_configure_hook(var, inheritable, handler_class,
    env_var)` instantiates `handler_class()` fresh whenever both `env_var`
    is truthy and the contextvar isn't otherwise set. This sidesteps
    contextvar-inheritance gotchas across async/threaded boundaries.
    All handler instances share class-level singleton state (queue +
    drain thread) so we don't multiply background workers.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
from typing import Any

__version__ = "0.2.0"

_DEFAULT_ENDPOINT = "http://127.0.0.1:8740"
_QUEUE_MAX = 1000
_POST_TIMEOUT_S = 1.0

# Env var the registered hook gates on. Set to "1" by attach() so LangChain
# fires our handler on every CallbackManager.configure() call.
_ACTIVE_ENV = "CHIMERA_OBSERVER_ACTIVE"

_attached = False
_attach_lock = threading.Lock()


def _derive_project() -> str:
    """Project name from CHIMERA_PROJECT env, else cwd basename."""
    explicit = os.environ.get("CHIMERA_PROJECT", "").strip()
    if explicit:
        return explicit
    try:
        return os.path.basename(os.getcwd()) or "unknown"
    except OSError:
        return "unknown"


# Singleton state — shared across every ChimeraTracer instance LangChain
# might construct in any context. Lazily initialized on first use.
_singleton_lock = threading.Lock()
_q: queue.Queue | None = None
_drain_thread: threading.Thread | None = None
_endpoint: str = ""
_project: str = ""


def _ensure_singleton() -> None:
    global _q, _drain_thread, _endpoint, _project
    if _q is not None:
        return
    with _singleton_lock:
        if _q is not None:
            return
        _endpoint = os.environ.get("CHIMERA_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")
        _project = _derive_project()
        _q = queue.Queue(maxsize=_QUEUE_MAX)

        def _drain() -> None:
            while True:
                try:
                    payload = _q.get()  # type: ignore[union-attr]
                except Exception:
                    return
                try:
                    data = json.dumps(payload, default=str).encode("utf-8")
                    req = urllib.request.Request(
                        _endpoint + "/api/heartbeat",
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as _:
                        pass
                except Exception:
                    pass  # silent — never block app on telemetry

        _drain_thread = threading.Thread(target=_drain, daemon=True, name="chimera-observer")
        _drain_thread.start()


def _enqueue(event: str, run_id: Any, parent_run_id: Any = None,
             name: str | None = None, extra: dict | None = None) -> None:
    _ensure_singleton()
    try:
        _q.put_nowait({  # type: ignore[union-attr]
            "project": _project,
            "run_id": str(run_id) if run_id is not None else None,
            "parent_run_id": str(parent_run_id) if parent_run_id is not None else None,
            "name": name,
            "event": event,
            "ts": time.time(),
            "extra": extra,
        })
    except queue.Full:
        pass


def _try_get_handler_class():
    """Return a BaseCallbackHandler subclass, or None if langchain is missing.

    Defined as a function so we don't pay the langchain_core import cost
    when the venv has langchain installed but the app doesn't import it
    during startup.
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:
        return None

    class ChimeraTracer(BaseCallbackHandler):  # type: ignore[misc]
        """LangChain global callback — posts every node/LLM/tool event.

        Per-instance __init__ is empty; all state lives in the module-level
        singleton (queue + drain thread). LangChain may instantiate this
        class many times across contexts; each instance is a thin shim
        feeding the same queue.
        """

        # Chain (node) events
        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
            try:
                name = (
                    (kwargs.get("name") if isinstance(kwargs, dict) else None)
                    or (serialized.get("name") if isinstance(serialized, dict) else None)
                    or (
                        serialized.get("id", [None])[-1]
                        if isinstance(serialized, dict) and serialized.get("id")
                        else None
                    )
                )
                _enqueue("chain_start", run_id, parent_run_id, name=name)
            except Exception:
                pass

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
            try:
                _enqueue("chain_end", run_id, parent_run_id)
            except Exception:
                pass

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            try:
                _enqueue("chain_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

        # LLM events
        def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
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

        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
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

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
            try:
                extra = {}
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

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            try:
                _enqueue("llm_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

        # Tool events
        def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
            try:
                name = serialized.get("name") if isinstance(serialized, dict) else None
                _enqueue("tool_start", run_id, parent_run_id, name=name)
            except Exception:
                pass

        def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
            try:
                _enqueue("tool_end", run_id, parent_run_id)
            except Exception:
                pass

        def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            try:
                _enqueue("tool_error", run_id, parent_run_id,
                         extra={"error": str(error)[:500]})
            except Exception:
                pass

    return ChimeraTracer


def attach() -> None:
    """Register the chimera tracer as a global LangChain callback.

    Idempotent. Safe to call multiple times. Silent on every failure mode
    (langchain missing, registration API changed, etc.) — apps must not
    break because of telemetry setup.
    """
    global _attached
    with _attach_lock:
        if _attached:
            return
        _attached = True

    if os.environ.get("CHIMERA_OBSERVER_DISABLE", "").lower() in ("1", "true", "yes"):
        return

    handler_class = _try_get_handler_class()
    if handler_class is None:
        return  # langchain not in this venv

    # Pre-warm singleton so the queue + drain thread exist before any callback fires
    _ensure_singleton()

    # Register the handler class with LangChain. The (handler_class, env_var)
    # combo means: "every time CallbackManager.configure() runs and CHIMERA_
    # OBSERVER_ACTIVE env is truthy, instantiate handler_class and add it to
    # the inheritable callbacks list." Each instance is a thin shim — they
    # all funnel into the module-level singleton queue.
    try:
        from contextvars import ContextVar
        from langchain_core.tracers.context import register_configure_hook  # type: ignore[import-not-found]

        var: ContextVar = ContextVar("chimera_handlers", default=None)
        os.environ[_ACTIVE_ENV] = "1"
        register_configure_hook(
            var,
            inheritable=True,
            handler_class=handler_class,
            env_var=_ACTIVE_ENV,
        )
    except Exception:
        # If registration fails, the handler simply doesn't fire. App is
        # unaffected. We don't print anything so we don't pollute startup.
        pass
