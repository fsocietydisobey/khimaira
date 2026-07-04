"""Class-invariant test for the shared-JSON torn-write bug class.

Bug class: every shared-JSON read-modify-write writer in this module (and
the separate-OS-process PostToolUse hook) used to persist via a bare
`path.write_text(json.dumps(...))` -- a truncate-then-write that is NOT
atomic. A concurrent reader (including another writer's own
read-modify-write cycle) hitting the file mid-write sees an empty/partial
file, which the `except (OSError, JSONDecodeError): existing = {}` fallback
silently treats as "no existing record" -- dropping every field the file
previously held. Two known instances of this class:

  1. status.json (per-session) -- `name` is the most visible casualty
     because it lives ONLY there; a SessionStart re-fire (compact / clear /
     resume) triggers exactly the multi-writer burst (daemon slot/window/
     ppid writers plus the hook's heartbeat writer, a genuinely separate OS
     process) that reproduces the drop.
  2. slot-registry.json (cross-session, shared by EVERY roster session's
     set_session_slot() call) -- same non-atomic write_text pattern, same
     exposure, just a shared file instead of a per-session one.

Fix: every writer routes through `_atomic_write_json` (unique-per-call tmp +
`Path.replace()`, POSIX-atomic same-directory rename) -- `_write_status_atomic`
and `_atomic_merge_status` for status.json, `_write_slot_registry` for the
slot registry. The hook's own duplicate writer (`_write_sse_heartbeat` in
post_tool_use.py, which deliberately avoids importing khimaira.monitor.sessions)
got the same tmp+replace treatment inline, since it's the cross-process half
of the status.json race.

Layers:
  1. Structural -- no writer to either shared JSON file may call
     `.write_text(...)` directly on the real path; only a tmp-suffixed
     staging path may be write_text'd, and only en route to a `.replace(...)`.
  2. Behavioral -- the actual class invariants:
       a. once set_name() has fired, `name` must NEVER be absent from
          status.json, no matter how many other writers (including a
          genuinely separate OS process) race against it.
       b. slot-registry.json must NEVER be left corrupt/unparseable under
          concurrent cross-process writers.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import khimaira.hooks.post_tool_use as hook_mod
import khimaira.monitor.sessions as sessions_mod

_WORKER = Path(__file__).parent / "_status_race_worker.py"
_SLOT_WORKER = Path(__file__).parent / "_slot_registry_race_worker.py"

# Filename suffixes tracked via the `<expr> / "<suffix>"` binding pattern
# (e.g. `_session_dir(session_id) / "status.json"`).
_TRACKED_PATH_SUFFIXES = {"status.json"}

# Path-accessor function calls tracked via the `<name> = <accessor>()`
# binding pattern (slot-registry.json has no per-call literal join --
# it's built once inside `_slot_registry_path()` and reused via that call).
_TRACKED_PATH_ACCESSORS = {"_slot_registry_path"}


def _tracked_path_var_names(node: ast.FunctionDef) -> set[str]:
    """Names locally bound to a shared-JSON path this test protects: either
    `<expr> / "status.json"` (per-session status file) or a call to
    `_slot_registry_path()` (the shared cross-session registry).

    Precise on purpose: some functions (e.g. `delete_session`) touch
    status.json for an unrelated READ alongside a write_text of a totally
    different file (an archive JSON) -- flagging every write_text in any
    function that merely mentions status.json would false-positive on those.
    Only variables actually assigned one of the tracked paths are in scope.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Assign):
            continue
        value = sub.value
        is_suffix_bind = (
            isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Div)
            and isinstance(value.right, ast.Constant)
            and value.right.value in _TRACKED_PATH_SUFFIXES
        )
        is_accessor_bind = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _TRACKED_PATH_ACCESSORS
        )
        if is_suffix_bind or is_accessor_bind:
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _is_unsafe_write_text_call(call_node: ast.Call, tracked_names: set[str]) -> str | None:
    """Return the offending target's description if `call_node` is a
    `.write_text(...)` call directly on a tracked shared-JSON path -- either
    a Name bound to one (`path.write_text(...)`) or a chained accessor call
    (`_slot_registry_path().write_text(...)`). None if safe / irrelevant.
    """
    if not (isinstance(call_node.func, ast.Attribute) and call_node.func.attr == "write_text"):
        return None
    target = call_node.func.value
    if isinstance(target, ast.Name) and target.id in tracked_names:
        return f"{target.id}.write_text(...)"
    if (
        isinstance(target, ast.Call)
        and isinstance(target.func, ast.Name)
        and target.func.id in _TRACKED_PATH_ACCESSORS
    ):
        return f"{target.func.id}().write_text(...)"
    return None


def _find_unsafe_status_writes(source: str, filename: str) -> list[str]:
    """Return violation descriptions for any `.write_text(...)` call made
    directly on a variable (or chained accessor call) bound to a tracked
    shared-JSON file's real path (as opposed to a tmp/staging path headed
    for `.replace(...)`) -- i.e. a bare, non-atomic write to the live file.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        tracked_names = _tracked_path_var_names(node)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            offender = _is_unsafe_write_text_call(sub, tracked_names)
            if offender is not None:
                violations.append(
                    f"{filename}:{node.name} (line {sub.lineno}): "
                    f"{offender} writes directly to a shared JSON file's "
                    "real path without a tmp+replace atomic swap"
                )
    return violations


def test_no_bare_status_json_write_in_sessions_module():
    """Structural: sessions.py must not reintroduce a non-atomic writer to
    status.json OR slot-registry.json -- both shared-JSON files in this
    module are covered by the same scan."""
    src = inspect.getsource(sessions_mod)
    violations = _find_unsafe_status_writes(src, "sessions.py")
    assert violations == [], "\n".join(violations)


def test_no_bare_status_json_write_in_hook():
    """Structural: the hook's cross-process writer must stay atomic too."""
    src = inspect.getsource(hook_mod)
    violations = _find_unsafe_status_writes(src, "post_tool_use.py")
    assert violations == [], "\n".join(violations)


def _spawn_worker(kind: str, session_id: str, iterations: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), kind, session_id, str(iterations)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_name_survives_concurrent_cross_process_writers(isolated_state):
    """The class invariant: once set, `name` is NEVER absent from
    status.json -- regardless of concurrent daemon-side writers AND a
    genuinely separate OS process (the PostToolUse hook's own heartbeat
    writer) racing against it.

    Pre-fix this reliably drops `name` (non-atomic truncate-then-write races
    with a concurrent reader). Post-fix (tmp + os.replace on every writer)
    the drop must never happen. Verified both ways during implementation
    (see khimaira-code-deep report): fails against the pre-fix code, passes
    against the post-fix code.
    """
    sid = f"race-session-{os.getpid()}-{threading.get_ident()}"
    isolated_state.set_name(sid, "griffin-agent-race")

    status_path = isolated_state._session_dir(sid) / "status.json"
    assert json.loads(status_path.read_text())["name"] == "griffin-agent-race"

    env = os.environ.copy()  # carries XDG_STATE_HOME set by isolated_state

    iterations = 6000
    kinds = ["window", "slot", "ppid", "heartbeat"]
    procs = [_spawn_worker(kind, sid, iterations, env) for kind in kinds]

    drop: dict = {}
    stop = threading.Event()

    def watcher() -> None:
        while not stop.is_set():
            try:
                raw = status_path.read_text(encoding="utf-8")
                rec = json.loads(raw)
                if "name" not in rec:
                    drop["record"] = rec
                    stop.set()
                    return
            except (OSError, json.JSONDecodeError):
                # A torn read on the WATCHER's side is expected pre-fix and
                # is not itself the bug -- the bug is a writer PERSISTING a
                # nameless record. Ignore and keep polling.
                pass

    watcher_thread = threading.Thread(target=watcher, daemon=True)
    watcher_thread.start()

    try:
        for p in procs:
            p.wait(timeout=120)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    time.sleep(0.05)  # one more beat for the watcher to catch a late drop
    stop.set()
    watcher_thread.join(timeout=5)

    if "record" not in drop:
        try:
            rec = json.loads(status_path.read_text(encoding="utf-8"))
            if "name" not in rec:
                drop["record"] = rec
        except (OSError, json.JSONDecodeError):
            pass

    assert "record" not in drop, (
        "class invariant violated: `name` dropped from status.json under "
        f"concurrent writers -- observed record: {drop.get('record')}"
    )


def _spawn_slot_worker(
    slot_prefix: str, sid_prefix: str, iterations: int, env: dict
) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_SLOT_WORKER), slot_prefix, sid_prefix, str(iterations)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_slot_registry_survives_concurrent_cross_process_writers(isolated_state):
    """The sibling class invariant: slot-registry.json -- a file shared
    across EVERY roster session (written by every set_session_slot() call,
    not just one session's own) -- must NEVER be left corrupt/unparseable
    under concurrent cross-process writers.

    Unlike status.json (where a single session's own writes race each
    other), this file's exposure is cross-session: any two sessions
    registering slots at the same moment (a roster-wide SessionStart burst)
    race on the SAME file. The invariant that matters here is narrower than
    status.json's "field never dropped" (losing a stale slot registration to
    a concurrent update is expected, last-writer-wins, read-modify-write
    behavior) -- what must NEVER happen is the file becoming invalid JSON,
    which is exactly what the shared-tmp-filename collision produces (see
    _atomic_write_json's docstring) and would corrupt slot resolution for
    every session, not just one.
    """
    registry_path = isolated_state._slot_registry_path()
    # slot-registry.json lives in _BASE_DIR.parent, which normally gets
    # created as a side effect of a session dir being created first. This
    # test never touches a per-session dir, so create it explicitly --
    # otherwise the very first _atomic_write_json call fails on a missing
    # parent dir and the test would pass vacuously (no file ever written).
    isolated_state._BASE_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()  # carries XDG_STATE_HOME set by isolated_state

    iterations = 4000
    procs = [
        _spawn_slot_worker("agent", "sid-a", iterations, env),
        _spawn_slot_worker("agent", "sid-b", iterations, env),
        _spawn_slot_worker("agent", "sid-c", iterations, env),
        _spawn_slot_worker("agent", "sid-d", iterations, env),
    ]

    corrupt: dict = {}
    stop = threading.Event()

    def watcher() -> None:
        while not stop.is_set():
            if not registry_path.exists():
                continue
            try:
                raw = registry_path.read_text(encoding="utf-8")
                json.loads(raw)
            except json.JSONDecodeError:
                corrupt["raw"] = raw
                stop.set()
                return
            except OSError:
                # Benign TOCTOU between exists() and read_text(); not itself
                # the bug we're checking for.
                pass

    watcher_thread = threading.Thread(target=watcher, daemon=True)
    watcher_thread.start()

    try:
        for p in procs:
            p.wait(timeout=120)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    time.sleep(0.05)  # one more beat for the watcher to catch a late corruption
    stop.set()
    watcher_thread.join(timeout=5)

    if "raw" not in corrupt and registry_path.exists():
        raw = registry_path.read_text(encoding="utf-8")
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            corrupt["raw"] = raw

    assert "raw" not in corrupt, (
        "class invariant violated: slot-registry.json left unparseable "
        f"under concurrent writers -- raw content: {corrupt.get('raw')!r}"
    )
