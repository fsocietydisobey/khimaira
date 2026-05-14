"""Console event capture and buffering.

Subscribes to Runtime.consoleAPICalled and Runtime.exceptionThrown events
via CDP, buffers them, and provides retrieval with filtering.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from specter.browser.connection import CDPConnection
from specter.config import SpecterConfig


@dataclass(frozen=True)
class ConsoleEntry:
    """A single console event."""

    timestamp: float
    level: str  # log, warn, error, info, debug
    text: str
    source: str  # url:line
    stack_trace: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "level": self.level,
            "text": self.text,
            "source": self.source,
        }
        if self.stack_trace:
            d["stack_trace"] = self.stack_trace
        return d


@dataclass(frozen=True)
class ExceptionEntry:
    """An unhandled JavaScript exception."""

    timestamp: float
    message: str
    source: str
    line: int
    column: int
    stack_trace: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "message": self.message,
            "source": self.source,
            "line": self.line,
            "column": self.column,
            "stack_trace": self.stack_trace,
        }


class ConsoleCapture:
    """Captures and buffers browser console events."""

    def __init__(self, config: SpecterConfig) -> None:
        self._console_buffer: deque[ConsoleEntry] = deque(maxlen=config.max_buffer_size)
        self._exception_buffer: deque[ExceptionEntry] = deque(maxlen=config.max_buffer_size)

    def register(self, connection: CDPConnection) -> None:
        """Register CDP event handlers for console capture."""
        connection.on("Runtime.consoleAPICalled", self._on_console)
        connection.on("Runtime.exceptionThrown", self._on_exception)

    async def enable(self, connection: CDPConnection) -> None:
        """Enable the Runtime domain so events start flowing."""
        await connection.send("Runtime.enable")

    def get_logs(
        self,
        level: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve buffered console entries.

        Args:
            level: Filter by level (log, warn, error, info, debug).
            since: Only entries after this Unix timestamp.
            limit: Max entries to return.

        Returns:
            List of console entry dicts, newest first.
        """
        entries = list(self._console_buffer)

        if level:
            entries = [e for e in entries if e.level == level]
        if since:
            entries = [e for e in entries if e.timestamp >= since]

        return [e.to_dict() for e in entries[-limit:]]

    def get_errors(self, since: float | None = None, limit: int = 50) -> list[dict]:
        """Retrieve buffered exception entries.

        Args:
            since: Only entries after this Unix timestamp.
            limit: Max entries to return.

        Returns:
            List of exception entry dicts, newest first.
        """
        entries = list(self._exception_buffer)

        if since:
            entries = [e for e in entries if e.timestamp >= since]

        return [e.to_dict() for e in entries[-limit:]]

    def clear(self) -> int:
        """Clear all buffers. Returns the number of entries cleared."""
        count = len(self._console_buffer) + len(self._exception_buffer)
        self._console_buffer.clear()
        self._exception_buffer.clear()
        return count

    def _on_console(self, params: dict) -> None:
        """Handle Runtime.consoleAPICalled events."""
        level = params.get("type", "log")
        args = params.get("args", [])

        # Stringify the console arguments
        text_parts: list[str] = []
        for arg in args:
            if arg.get("type") == "string":
                text_parts.append(arg.get("value", ""))
            elif "description" in arg:
                text_parts.append(arg["description"])
            elif "value" in arg:
                text_parts.append(str(arg["value"]))
            else:
                text_parts.append(f"[{arg.get('type', 'unknown')}]")

        text = " ".join(text_parts)

        # Extract source location from stack trace
        source = ""
        stack_trace = None
        st = params.get("stackTrace", {})
        call_frames = st.get("callFrames", [])
        if call_frames:
            frame = call_frames[0]
            source = f"{frame.get('url', '')}:{frame.get('lineNumber', 0)}"
            if level == "error":
                stack_trace = "\n".join(
                    f"  at {f.get('functionName', '(anonymous)')} "
                    f"({f.get('url', '')}:{f.get('lineNumber', 0)}:{f.get('columnNumber', 0)})"
                    for f in call_frames
                )

        self._console_buffer.append(
            ConsoleEntry(
                timestamp=time.time(),
                level=level,
                text=text,
                source=source,
                stack_trace=stack_trace,
            )
        )

    def _on_exception(self, params: dict) -> None:
        """Handle Runtime.exceptionThrown events."""
        details = params.get("exceptionDetails", {})
        exception = details.get("exception", {})

        message = exception.get("description", details.get("text", "Unknown error"))
        url = details.get("url", "")
        line = details.get("lineNumber", 0)
        column = details.get("columnNumber", 0)

        # Build stack trace
        stack_trace = message
        st = details.get("stackTrace", {})
        call_frames = st.get("callFrames", [])
        if call_frames:
            stack_trace = message + "\n" + "\n".join(
                f"  at {f.get('functionName', '(anonymous)')} "
                f"({f.get('url', '')}:{f.get('lineNumber', 0)}:{f.get('columnNumber', 0)})"
                for f in call_frames
            )

        self._exception_buffer.append(
            ExceptionEntry(
                timestamp=time.time(),
                message=message,
                source=url,
                line=line,
                column=column,
                stack_trace=stack_trace,
            )
        )
