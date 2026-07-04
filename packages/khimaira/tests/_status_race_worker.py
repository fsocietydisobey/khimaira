"""Standalone worker process for the status.json atomic-write class-invariant
test (`test_status_json_atomic_class.py`). Spawned via `subprocess.Popen` so
the race is genuinely CROSS-PROCESS -- reproducing the actual SessionStart
burst where daemon-side writers (sessions.py: set_session_window/slot/ppid)
and the PostToolUse hook (a SEPARATE OS process; hooks/post_tool_use.py)
read-modify-write the same session's status.json concurrently.

Not a pytest test file itself -- imported only as a subprocess entry point.

argv: <kind> <session_id> <iterations>
  kind: one of "window", "slot", "ppid", "heartbeat"
"""

from __future__ import annotations

import sys


def main() -> int:
    kind, session_id, iterations_s = sys.argv[1], sys.argv[2], sys.argv[3]
    iterations = int(iterations_s)

    if kind == "heartbeat":
        # Exercises the hook's OWN status.json writer -- the cross-process
        # half of the bug class. Deliberately imported the same way the real
        # PostToolUse hook does (it never imports khimaira.monitor.sessions).
        from khimaira.hooks import post_tool_use as hook

        for _ in range(iterations):
            hook._write_sse_heartbeat(session_id)
        return 0

    from khimaira.monitor import sessions as sessions_mod

    for i in range(iterations):
        if kind == "window":
            sessions_mod.set_session_window(session_id, 1000 + i)
        elif kind == "slot":
            sessions_mod.set_session_slot(session_id, f"inst-race:agent-{i % 3}")
        elif kind == "ppid":
            sessions_mod.set_session_ppid(session_id, 20000 + i)
        else:
            raise ValueError(f"unknown worker kind {kind!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
