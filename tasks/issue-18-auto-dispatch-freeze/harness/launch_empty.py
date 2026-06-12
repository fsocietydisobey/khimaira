"""Stand up an ISOLATED, FORKED khimaira monitor daemon to reproduce #18 offline.

Uses the REAL daemonize_and_serve (double-fork + setsid + stdio→logfile, no TTY)
— the exact prod launch path my in-process uvicorn.Server lab never replicated.
Isolated via temp XDG_STATE_HOME/XDG_DATA_HOME (own PID file, log, state) + test
port 8799, so it runs alongside prod without touching muther's daemon.

Joseph's sequence step 1: EMPTY state. If auto_dispatch freezes here (while the
A/B/D canaries tick) with zero sessions, the FORKED LAUNCH is the trigger.
"""

import os

ISO = "/tmp/ad_fork"
os.environ["XDG_STATE_HOME"] = f"{ISO}/state"
os.environ["XDG_DATA_HOME"] = f"{ISO}/data"
os.environ["XDG_CONFIG_HOME"] = f"{ISO}/config"
os.environ["XDG_CACHE_HOME"] = f"{ISO}/cache"
os.environ["KHIMAIRA_DEBUG_CANARY"] = "1"
os.environ["KHIMAIRA_AUTO_DISPATCH_S"] = "5"
os.environ["KHIMAIRA_ROSTER_WATCH_S"] = "5"

for d in ("state", "data", "config", "cache"):
    os.makedirs(f"{ISO}/{d}/khimaira", exist_ok=True)

# Import AFTER env is set so paths.py derives PID_FILE/LOG_FILE from the temp XDG.
from khimaira.monitor.daemon import daemonize_and_serve  # noqa: E402

pid = daemonize_and_serve(port=8799)
print(f"FORKED-TEST-DAEMON PID={pid} port=8799 log={ISO}/state/khimaira/monitor.log")
