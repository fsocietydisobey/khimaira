"""Class-invariant tests for TM1 daemon-auth (task-6487, Phase B.1).

Three layers per spec:
A. STRUCTURAL — every PRIVILEGE_PATHS endpoint has require_actor in its dependency
   chain. Catches future privilege endpoints added without the guard regardless of
   which specific path they enter through (close-the-class-not-the-instance).
B. POSITIVE — a legit X-Session-ID header → privilege op (grant-role) succeeds.
C. BEHAVIORAL — body-only authority (no header) → reject.
   xfail in B.1 (warn+fallback → 200); enabled at B.2 flip.
D. DOCUMENTED OUT-OF-SCOPE — both-spoofed curl (header+body=victim) PASSES =
   accepted same-uid residual (TM2 declined per SECURITY.md).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_chats_api.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    router = api_mod.build_router()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    for sid in ("alice", "bob"):
        sd = sessions_mod._session_dir(sid)
        (sd / "status.json").write_text(
            json.dumps({"status": "implementing", "name": sid}), encoding="utf-8"
        )

    yield client, chats_mod, router, sessions_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)
    importlib.reload(api_mod)


# ---------------------------------------------------------------------------
# A. STRUCTURAL — require_actor wired to every privilege endpoint
# ---------------------------------------------------------------------------

# (method, path) pairs — only the privilege-granting HTTP methods, not read-only GETs.
PRIVILEGE_ENDPOINTS: set[tuple[str, str]] = {
    ("POST", "/chats/{chat_id}/grant-role"),
    ("POST", "/chats/{chat_id}/invite"),
    ("DELETE", "/chats/{chat_id}/members/{target_session_id}"),
    ("POST", "/chats/{chat_id}/tasks/{task_id}/override-verdict"),
    ("POST", "/chats/{chat_id}/resume-master"),
    ("POST", "/chats/{chat_id}/transfer-membership"),
    ("POST", "/chats/{chat_id}/tasks/{task_id}/signal-start"),
    ("POST", "/chats/{chat_id}/tasks/{task_id}/status"),
    ("POST", "/chats/{chat_id}/tasks/{task_id}/verdict"),
    ("DELETE", "/chats/{chat_id}"),
    ("POST", "/chats/{chat_id}/assign-batch"),
    ("POST", "/chats/{chat_id}/tasks"),
    ("POST", "/chats/{chat_id}/messages"),
}


def _dependency_names(route) -> set[str]:
    """Collect all dependency function names for a FastAPI route."""
    names: set[str] = set()
    # FastAPI resolves dependencies onto `route.dependant` (not `route.endpoint.dependant`)
    for source in (route, getattr(route, "endpoint", None)):
        if source is None:
            continue
        dependant = getattr(source, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            call = getattr(dep, "call", None)
            if call and callable(call):
                names.add(call.__name__)
            inner = getattr(dep, "dependant", None)
            if inner:
                for subdep in inner.dependencies:
                    subcall = getattr(subdep, "call", None)
                    if subcall:
                        names.add(subcall.__name__)
    return names


def test_require_actor_present_on_all_privilege_endpoints(auth_api):
    """STRUCTURAL: every PRIVILEGE_PATHS endpoint must use require_actor.

    Fails if a future privilege endpoint is added without the guard.
    """
    _, _, router, _ = auth_api
    missing = []
    for route in router.routes:
        if not hasattr(route, "path"):
            continue
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            if (method.upper(), route.path) not in PRIVILEGE_ENDPOINTS:
                continue
            dep_names = _dependency_names(route)
            if "require_actor" not in dep_names:
                missing.append(f"{method} {route.path}")

    assert not missing, (
        f"STRUCTURAL INVARIANT BROKEN — {len(missing)} privilege endpoint(s) "
        f"missing require_actor dependency:\n" + "\n".join(f"  {m}" for m in missing)
    )


# ---------------------------------------------------------------------------
# B. POSITIVE — legit X-Session-ID → grant-role succeeds
# ---------------------------------------------------------------------------


def test_grant_role_with_valid_session_id_header_succeeds(auth_api):
    """B.POSITIVE: master calling grant-role with X-Session-ID header → 200."""
    client, chats_mod, _, _ = auth_api

    room = chats_mod.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    chats_mod.accept(chat_id, "bob")

    resp = client.post(
        f"/api/chats/{chat_id}/grant-role",
        json={"by_session_id": "alice", "target_session_id": "bob", "role": "agent"},
        headers={"X-Session-ID": "alice"},
    )
    assert resp.status_code == 200
    assert resp.json()["member_roles"].get("bob") == "agent"


# ---------------------------------------------------------------------------
# C. BEHAVIORAL — body-only authority (no header) → 401/403
# xfail in B.1 (warn+fallback currently returns 200).
# Remove xfail at B.2 flip (hard-reject).
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="B.1 warn+fallback: header absent → fallback to body → 200. "
    "Remove xfail at B.2 hard-reject flip (warns→0 signal).",
    strict=False,
)
def test_grant_role_without_header_rejected(auth_api):
    """C.BEHAVIORAL (B.2 end-state): body authority + no X-Session-ID → 401/403.

    xfail in B.1 because warn+fallback allows the call through.
    At B.2 flip: this becomes the load-bearing security assertion.
    """
    client, chats_mod, _, _ = auth_api

    room = chats_mod.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    chats_mod.accept(chat_id, "bob")

    resp = client.post(
        f"/api/chats/{chat_id}/grant-role",
        json={"by_session_id": "alice", "target_session_id": "bob", "role": "agent"},
        # deliberately NO X-Session-ID header
    )
    # B.2: expect 401 (header absent) or 403 (non-master header)
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 when X-Session-ID absent, got {resp.status_code}. "
        "B.2 flip may not be active yet."
    )


# ---------------------------------------------------------------------------
# D. DOCUMENTED OUT-OF-SCOPE (comment-only, no assertion)
# ---------------------------------------------------------------------------

# A both-spoofed curl (body by_session_id=<victim> + header X-Session-ID=<victim>)
# PASSES under TM1. This is the accepted same-uid residual: the daemon cannot
# distinguish a legitimate caller from a malicious same-uid process that reads
# the victim's UUID from chat history and sets both fields.
#
# This is OUT OF SCOPE under the single-operator localhost model (SECURITY.md).
# Closing it requires OS-level isolation (separate uids / SO_PEERCRED) or TM2
# (per-session secrets), both declined as disproportionate. Do NOT write a test
# asserting this passes — that would encode the weakness as a regression target.
