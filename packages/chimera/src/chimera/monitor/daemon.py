"""Daemonization via os.fork() + os.setsid() + stdio redirect.

No `python-daemon` dep — that library is maintenance-abandoned (last release
2021) and has known Python 3.12+ compat issues. The double-fork dance below
is small enough to own outright. Reuses chimera's PID/lock conventions via
the `paths` module.
"""

import os
import signal
import sys
from typing import NoReturn

from .paths import LOG_FILE, PID_FILE, ensure_dirs


def daemonize_and_serve(*, port: int) -> int:
    """Double-fork into a daemon, write PID file, then serve.

    Parent returns the daemon PID. The grandchild becomes the daemon and
    never returns (it calls `serve()` and runs uvicorn until SIGTERM).
    """
    ensure_dirs()

    # First fork — detach from invoking shell
    pid = os.fork()
    if pid > 0:
        # In the original parent: wait for the intermediate child to exit so
        # the grandchild's PID is the one we record. We pass the grandchild
        # PID back via the PID file (the intermediate child writes it before
        # exiting).
        os.waitpid(pid, 0)
        try:
            return int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            return -1

    # Intermediate child
    os.setsid()
    pid = os.fork()
    if pid > 0:
        # Intermediate child writes the grandchild PID and exits
        PID_FILE.write_text(str(pid))
        os._exit(0)

    # Grandchild — the actual daemon
    _redirect_stdio()
    _install_signal_handlers()
    # Re-write PID just in case the intermediate parent's write raced.
    PID_FILE.write_text(str(os.getpid()))

    from .server import serve

    try:
        serve(port=port)
    finally:
        # Best-effort PID cleanup; fine if it was already removed by `stop`
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass

    os._exit(0)


def _redirect_stdio() -> None:
    """Detach stdio from the controlling terminal; redirect to log file."""
    sys.stdout.flush()
    sys.stderr.flush()

    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull_fd)
    os.close(log_fd)


def _install_signal_handlers() -> None:
    """SIGTERM triggers a clean shutdown by raising SystemExit.

    uvicorn registers its own SIGTERM handler in the running event loop; this
    handler is a backstop so a SIGTERM during startup (before uvicorn binds)
    still terminates the process.
    """

    def _handler(signum: int, frame) -> NoReturn:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
