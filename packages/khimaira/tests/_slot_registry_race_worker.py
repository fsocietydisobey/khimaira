"""Standalone worker process for the slot-registry.json atomic-write
class-invariant test (`test_status_json_atomic_class.py`). Spawned via
`subprocess.Popen` so writers race across genuinely SEPARATE OS processes --
mirroring how every roster session calls `set_session_slot()` (and therefore
`_update_slot_registry()`) at SessionStart, all against the same shared,
cross-session `slot-registry.json`.

Not a pytest test file itself -- imported only as a subprocess entry point.

argv: <slot_prefix> <session_id_prefix> <iterations>
"""

from __future__ import annotations

import sys


def main() -> int:
    slot_prefix, sid_prefix, iterations_s = sys.argv[1], sys.argv[2], sys.argv[3]
    iterations = int(iterations_s)

    from khimaira.monitor import sessions as sessions_mod

    for i in range(iterations):
        # A handful of shared slot keys -- not one-per-iteration -- so
        # concurrent workers genuinely contend on the SAME registry entries,
        # not just the same file with disjoint keys.
        slot = f"{slot_prefix}-{i % 3}"
        sid = f"{sid_prefix}-{i}"
        sessions_mod._update_slot_registry(slot, sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
