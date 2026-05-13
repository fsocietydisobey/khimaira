"""khimaira observer — auto-injected into target apps' venvs.

Stdlib-only. Drop into a venv's site-packages and pair with khimaira_observer.pth
to auto-load on every Python interpreter start. Registers a global LangChain
callback handler that POSTs hierarchical run events to the khimaira daemon.

Wire protocol (khimaira-defined, vendor-neutral):
    POST {endpoint}/api/heartbeat
    {
        "project": "...",
        "run_id": "...",
        "parent_run_id": "..." | null,
        "name": "node_name" | "model_name" | None,
        "event": "chain_start" | "chain_end" | "llm_start" | "llm_end" |
                 "tool_start" | "tool_end" | "chain_error" | "llm_error" |
                 "tool_error" | "external_start" | "external_end" |
                 "external_error",
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

import contextlib
import json
import os
import queue
import threading
import time
import urllib.request
import uuid
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

__version__ = "0.4.1"

# App-level run correlation. Two paths to populate it:
#
# 1. AUTO (default, zero-touch): when LangChain fires on_chain_start with
#    parent_run_id=None (top-level chain — i.e. graph.invoke), our tracer
#    sets _correlation_id to that run_id. Every downstream event in the
#    same context (sub-chains, llms, tools, AND external HTTP calls
#    captured by our httpx/requests monkey-patches) inherits that
#    correlation_id via ContextVar. App code unchanged.
#
# 2. EXPLICIT (override): app calls `with tag_run(my_id)` to use a
#    domain-specific id (deliverable_id, business txn id, etc.) instead
#    of LangChain's UUID. tag_run wins over auto when both are present.
#
# Without correlation_id, querying "all events for app run X" required
# scanning every callback run in the heartbeat buffer (jeevy Phase A
# validation needed this — took ~150 sub-run scans to reconstruct one
# logical 3-page run).
_correlation_id: ContextVar[str | None] = ContextVar(
    "khimaira_correlation_id", default=None
)
# Tracks whether the current scope's correlation_id was set by tag_run
# (explicit) or auto-set by on_chain_start. Auto-set values get cleared
# on on_chain_end of the same top-level run; explicit values don't.
_correlation_auto: ContextVar[bool] = ContextVar(
    "khimaira_correlation_auto", default=False
)

_DEFAULT_ENDPOINT = "http://127.0.0.1:8740"
_QUEUE_MAX = 1000
_POST_TIMEOUT_S = 1.0

# Env var the registered hook gates on. Set to "1" by attach() so LangChain
# fires our handler on every CallbackManager.configure() call.
_ACTIVE_ENV = "KHIMAIRA_OBSERVER_ACTIVE"

_attached = False
_attach_lock = threading.Lock()


def _derive_project() -> str:
    """Project name from KHIMAIRA_PROJECT env, else cwd basename."""
    explicit = os.environ.get("KHIMAIRA_PROJECT", "").strip()
    if explicit:
        return explicit
    try:
        return os.path.basename(os.getcwd()) or "unknown"
    except OSError:
        return "unknown"


# Singleton state — shared across every KhimairaTracer instance LangChain
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
        _endpoint = os.environ.get("KHIMAIRA_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")
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

        _drain_thread = threading.Thread(target=_drain, daemon=True, name="khimaira-observer")
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
            "correlation_id": _correlation_id.get(),
        })
    except queue.Full:
        pass


@contextlib.contextmanager
def tag_run(correlation_id: str):
    """OPTIONAL: tag every observer event in this scope with a custom
    `correlation_id` (e.g. your business-domain id like deliverable_id,
    rather than LangChain's auto-assigned run UUID).

    **You usually don't need this.** v0.4.1+ auto-derives correlation_id
    from LangChain's top-level run_id (the one passed to on_chain_start
    when parent_run_id is None — i.e. when graph.invoke fires). All
    downstream events including external HTTP get tagged automatically
    via ContextVar inheritance. Zero app code changes.

    Use tag_run only when you want a domain-specific identifier instead
    of LangChain's UUID — e.g. you'd rather query by deliverable_id than
    by an opaque run_id. Explicit values win over auto when both are set.

    Example::

        with khimaira_observer.tag_run(deliverable_id):
            result = graph.invoke(state)
        # → all events for this graph run carry correlation_id=deliverable_id
        # → query: GET /api/heartbeats/{project}/by-correlation/{deliverable_id}

    ContextVar-based — propagates through async/await and to_thread
    boundaries automatically. Nested tag_runs supported (inner overrides
    outer for inner scope; outer restored on exit).
    """
    token = _correlation_id.set(correlation_id)
    auto_token = _correlation_auto.set(False)  # explicit
    try:
        yield correlation_id
    finally:
        _correlation_id.reset(token)
        _correlation_auto.reset(auto_token)


def set_correlation_id(correlation_id: str | None) -> None:
    """Set correlation_id for the current context without a context manager.

    Useful at process boundaries (FastAPI middleware, RQ worker entrypoint)
    where a `with` block doesn't fit. Pair with set_correlation_id(None) at
    the end of the request scope.
    """
    _correlation_id.set(correlation_id)


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

    class KhimairaTracer(BaseCallbackHandler):  # type: ignore[misc]
        """LangChain global callback — posts every node/LLM/tool event.

        Per-instance __init__ is empty; all state lives in the module-level
        singleton (queue + drain thread). LangChain may instantiate this
        class many times across contexts; each instance is a thin shim
        feeding the same queue.
        """

        # Chain (node) events
        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
            try:
                # Auto-correlation: when this is a top-level chain (no
                # parent), and no explicit tag_run is in scope, set
                # correlation_id to this run_id. Every sub-event in this
                # ContextVar scope (sub-chains, llms, tools, external
                # HTTP via httpx/requests monkey-patches) inherits it.
                # No app code changes needed — graph.invoke "just works."
                if parent_run_id is None and _correlation_id.get() is None:
                    _correlation_id.set(str(run_id))
                    _correlation_auto.set(True)

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
                # Clear auto-set correlation when the top-level chain
                # ends. Explicit tag_run values are not cleared (they're
                # managed by the context manager exit instead).
                if (
                    parent_run_id is None
                    and _correlation_auto.get()
                    and _correlation_id.get() == str(run_id)
                ):
                    _correlation_id.set(None)
                    _correlation_auto.set(False)
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

    return KhimairaTracer


def _should_skip_external(host: str) -> bool:
    """True if a hostname is loopback or khimaira's own endpoint (loop guard).

    We MUST skip our own daemon's host or the observer's POSTs would
    recursively trigger more heartbeats. Localhost/loopback gets skipped
    too — most of what flows there is dev infra (databases, redis, the
    khimaira daemon itself) and the noise/value ratio is bad.
    """
    if not host:
        return True
    h = host.lower()
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if h.startswith("127.") or h.startswith("10."):
        return True
    # Skip whatever host khimaira lives on (parsed from _endpoint at attach time)
    try:
        khimaira_host = urlsplit(_endpoint).hostname
        if khimaira_host and khimaira_host.lower() == h:
            return True
    except Exception:
        pass
    return False


def _emit_external_start(host: str, method: str, path: str) -> str:
    run_id = str(uuid.uuid4())
    _enqueue(
        "external_start",
        run_id,
        name=host,
        extra={"method": method, "path": path[:200]},
    )
    return run_id


def _emit_external_end(run_id: str, host: str, method: str, status: int, ms: int) -> None:
    _enqueue(
        "external_end",
        run_id,
        name=host,
        extra={"method": method, "status": status, "ms": ms},
    )


def _emit_external_error(run_id: str, host: str, method: str, error: str, ms: int) -> None:
    _enqueue(
        "external_error",
        run_id,
        name=host,
        extra={"method": method, "error": error[:300], "ms": ms},
    )


def _patch_httpx() -> None:
    """Wrap httpx.Client.send / httpx.AsyncClient.send to emit heartbeats.

    Idempotent — checks _khimaira_patched flag. No-op if httpx not installed.
    """
    try:
        import httpx
    except ImportError:
        return

    sync_send = getattr(httpx.Client, "send", None)
    if sync_send is not None and not getattr(sync_send, "_khimaira_patched", False):
        original_sync = sync_send

        def patched_sync_send(self, request, **kwargs):
            host = request.url.host or ""
            if _should_skip_external(host):
                return original_sync(self, request, **kwargs)
            run_id = _emit_external_start(host, request.method, request.url.path or "/")
            start = time.time()
            try:
                resp = original_sync(self, request, **kwargs)
                ms = int((time.time() - start) * 1000)
                _emit_external_end(run_id, host, request.method, resp.status_code, ms)
                return resp
            except Exception as exc:
                ms = int((time.time() - start) * 1000)
                _emit_external_error(run_id, host, request.method, repr(exc), ms)
                raise

        patched_sync_send._khimaira_patched = True  # type: ignore[attr-defined]
        httpx.Client.send = patched_sync_send  # type: ignore[method-assign]

    async_send = getattr(httpx.AsyncClient, "send", None)
    if async_send is not None and not getattr(async_send, "_khimaira_patched", False):
        original_async = async_send

        async def patched_async_send(self, request, **kwargs):
            host = request.url.host or ""
            if _should_skip_external(host):
                return await original_async(self, request, **kwargs)
            run_id = _emit_external_start(host, request.method, request.url.path or "/")
            start = time.time()
            try:
                resp = await original_async(self, request, **kwargs)
                ms = int((time.time() - start) * 1000)
                _emit_external_end(run_id, host, request.method, resp.status_code, ms)
                return resp
            except Exception as exc:
                ms = int((time.time() - start) * 1000)
                _emit_external_error(run_id, host, request.method, repr(exc), ms)
                raise

        patched_async_send._khimaira_patched = True  # type: ignore[attr-defined]
        httpx.AsyncClient.send = patched_async_send  # type: ignore[method-assign]


def _patch_langsmith_bypass() -> None:
    """No-op LangSmith client uploads when KHIMAIRA_DISABLE_LANGSMITH=true.

    LangChain's default tracer pings api.smith.langchain.com on every
    chain/llm/tool boundary — observed ~72 calls per LangGraph run in
    jeevy ingestion. If the user's only observability is khimaira's own
    HTTP instrumentation, those LangSmith calls are pure overhead:
    bandwidth + per-call latency + (potentially) cost on the LangSmith
    side.

    This patches langsmith.client.Client.create_run / update_run /
    multipart_ingest_runs to no-op. Apps that DO use LangSmith just
    don't set the env var and behavior is unchanged.

    Stdlib only — checks for langsmith installed, no-ops if absent. Each
    method patched independently so partial-API mismatches don't break
    the whole shim.
    """
    if os.environ.get("KHIMAIRA_DISABLE_LANGSMITH", "").lower() not in (
        "1", "true", "yes"
    ):
        return  # opt-in; default is unchanged behavior
    try:
        from langsmith.client import Client  # type: ignore[import-not-found]
    except ImportError:
        return

    def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    async def _async_noop(*args, **kwargs):  # noqa: ARG001
        return None

    for method_name in (
        "create_run",
        "update_run",
        "create_runs",
        "update_runs",
        "multipart_ingest_runs",
        "batch_ingest_runs",
    ):
        method = getattr(Client, method_name, None)
        if method is None:
            continue
        if getattr(method, "_khimaira_bypassed", False):
            continue
        replacement = _async_noop if _is_async_callable(method) else _noop
        replacement._khimaira_bypassed = True  # type: ignore[attr-defined]
        try:
            setattr(Client, method_name, replacement)
        except Exception:
            pass


def _is_async_callable(fn) -> bool:
    import asyncio
    import inspect
    return asyncio.iscoroutinefunction(fn) or inspect.iscoroutinefunction(fn)


def _patch_requests() -> None:
    """Wrap requests.Session.send to emit heartbeats. No-op if not installed."""
    try:
        import requests
    except ImportError:
        return

    send = getattr(requests.Session, "send", None)
    if send is None or getattr(send, "_khimaira_patched", False):
        return
    original = send

    def patched_send(self, request, **kwargs):
        try:
            host = urlsplit(request.url).hostname or ""
            method = request.method or "?"
            path = urlsplit(request.url).path or "/"
        except Exception:
            return original(self, request, **kwargs)
        if _should_skip_external(host):
            return original(self, request, **kwargs)
        run_id = _emit_external_start(host, method, path)
        start = time.time()
        try:
            resp = original(self, request, **kwargs)
            ms = int((time.time() - start) * 1000)
            _emit_external_end(run_id, host, method, resp.status_code, ms)
            return resp
        except Exception as exc:
            ms = int((time.time() - start) * 1000)
            _emit_external_error(run_id, host, method, repr(exc), ms)
            raise

    patched_send._khimaira_patched = True  # type: ignore[attr-defined]
    requests.Session.send = patched_send  # type: ignore[method-assign]


def attach() -> None:
    """Register the khimaira tracer as a global LangChain callback.

    Idempotent. Safe to call multiple times. Silent on every failure mode
    (langchain missing, registration API changed, etc.) — apps must not
    break because of telemetry setup.
    """
    global _attached
    with _attach_lock:
        if _attached:
            return
        _attached = True

    if os.environ.get("KHIMAIRA_OBSERVER_DISABLE", "").lower() in ("1", "true", "yes"):
        return

    handler_class = _try_get_handler_class()
    if handler_class is None:
        return  # langchain not in this venv

    # Pre-warm singleton so the queue + drain thread exist before any callback fires
    _ensure_singleton()

    # External HTTP instrumentation — captures Roboflow, OpenAI, Anthropic,
    # any outbound HTTP. Without this the dashboard goes dark whenever the
    # app is blocked on an external API since LangChain callbacks only fire
    # for in-process chain/llm/tool boundaries. Each patch is silent on its
    # own failure path.
    try:
        _patch_httpx()
    except Exception:
        pass
    try:
        _patch_requests()
    except Exception:
        pass
    try:
        _patch_langsmith_bypass()
    except Exception:
        pass

    # Register the handler class with LangChain. The (handle_class, env_var)
    # combo means: "every time CallbackManager.configure() runs and KHIMAIRA_
    # OBSERVER_ACTIVE env is truthy, instantiate handle_class and add it to
    # the inheritable callbacks list." Each instance is a thin shim — they
    # all funnel into the module-level singleton queue.
    #
    # Critical: we pass args POSITIONALLY. LangChain's kwarg is `handle_class`
    # (no 'r') and `env_var`; if upstream renames a kwarg, positional calls
    # keep working as long as the order is stable. v0.1.0 of this observer
    # set the contextvar to `[tracer]` directly, which LangChain's _configure
    # adds as a single handler — meaning self.handlers ends up containing
    # a list, and later iteration crashes with AttributeError. This pattern
    # avoids that: handle_class lets LangChain instantiate fresh per context.
    try:
        from contextvars import ContextVar
        from langchain_core.tracers.context import register_configure_hook  # type: ignore[import-not-found]

        var: ContextVar = ContextVar("khimaira_handlers", default=None)
        os.environ[_ACTIVE_ENV] = "1"
        register_configure_hook(var, True, handler_class, _ACTIVE_ENV)
    except Exception:
        # If registration fails, the handler simply doesn't fire. App is
        # unaffected. We don't print anything so we don't pollute startup.
        pass
