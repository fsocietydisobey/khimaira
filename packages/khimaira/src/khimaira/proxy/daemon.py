"""Proxy daemonization — mirrors monitor/daemon.py pattern."""

from __future__ import annotations

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

    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        try:
            return int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            return -1

    os.setsid()
    pid = os.fork()
    if pid > 0:
        PID_FILE.write_text(str(pid))
        os._exit(0)

    _redirect_stdio()
    _install_signal_handlers()
    PID_FILE.write_text(str(os.getpid()))

    from .server import serve

    try:
        serve(port=port)
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass

    os._exit(0)


def _redirect_stdio() -> None:
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
    def _handler(signum: int, frame) -> NoReturn:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
