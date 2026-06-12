import os
R = "/tmp/ad_fork_real"
os.environ["XDG_STATE_HOME"] = f"{R}/state"
os.environ["XDG_DATA_HOME"] = f"{R}/data"
os.environ["XDG_CONFIG_HOME"] = f"{R}/config"
os.environ["XDG_CACHE_HOME"] = f"{R}/cache"
os.environ["KHIMAIRA_DEBUG_CANARY"] = "1"
os.environ["KHIMAIRA_AUTO_DISPATCH_S"] = "5"
os.environ["KHIMAIRA_ROSTER_WATCH_S"] = "5"
# SAFETY: no kitty socket → real wake/reconcile can't inject into live windows
os.environ.pop("KITTY_LISTEN_ON", None)
os.environ.pop("KITTY_WINDOW_ID", None)
from khimaira.monitor.daemon import daemonize_and_serve  # noqa: E402
pid = daemonize_and_serve(port=8798)
print(f"FORKED-REAL-STATE-DAEMON PID={pid} port=8798 log={R}/state/khimaira/monitor.log")
