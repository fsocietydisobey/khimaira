"""Filesystem tools for LangGraph agent nodes.

These tools give agent nodes the ability to explore and read a codebase
without requiring the user to manually paste file contents. Each tool is
capped to prevent token explosion in agent context windows.

Limits:
    - read_file: 2000 lines max
    - glob_files: 200 results max
    - grep_content: 100 matching lines max
    - list_dir: no cap (single directory listing is bounded)
    - write_file: no cap (writes are inherently bounded by input)
"""

import fnmatch
import os
import re
from pathlib import Path

from langchain_core.tools import tool

# --- Caps to prevent token explosion ---
MAX_READ_LINES = 2000
MAX_GLOB_RESULTS = 200
MAX_GREP_LINES = 100


@tool
def read_file(file_path: str, offset: int = 0, limit: int = MAX_READ_LINES) -> str:
    """Read the contents of a file. Returns numbered lines.

    Args:
        file_path: Absolute or relative path to the file.
        offset: Line number to start reading from (0-based). Default 0.
        limit: Max number of lines to return. Default 2000.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: file not found: {path}"
    if not path.is_file():
        return f"Error: not a file: {path}"

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"Error reading {path}: {e}"

    # Apply offset and limit
    total = len(lines)
    start = min(offset, total)
    end = min(start + limit, total)
    selected = lines[start:end]

    # Format with line numbers (1-based for display)
    numbered = [f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)]
    header = f"# {path} ({total} lines total, showing {start + 1}-{end})"
    return header + "\n" + "\n".join(numbered)


@tool
def glob_files(pattern: str, directory: str = ".") -> str:
    """Find files matching a glob pattern. Returns up to 200 matching paths.

    Args:
        pattern: Glob pattern (e.g. '**/*.py', 'src/**/*.ts').
        directory: Root directory to search from. Default is current dir.
    """
    root = Path(directory).expanduser().resolve()
    if not root.exists():
        return f"Error: directory not found: {root}"

    matches = sorted(root.glob(pattern))
    # Filter out directories, keep only files
    files = [str(m.relative_to(root)) for m in matches if m.is_file()]

    if len(files) > MAX_GLOB_RESULTS:
        return (
            f"Found {len(files)} files (showing first {MAX_GLOB_RESULTS}):\n"
            + "\n".join(files[:MAX_GLOB_RESULTS])
        )

    if not files:
        return f"No files matching '{pattern}' in {root}"

    return f"Found {len(files)} files:\n" + "\n".join(files)


@tool
def grep_content(pattern: str, directory: str = ".", file_glob: str = "*") -> str:
    """Search file contents for a regex pattern. Returns up to 100 matching lines.

    Args:
        pattern: Regex pattern to search for (e.g. 'def classify', 'import.*langgraph').
        directory: Root directory to search from. Default is current dir.
        file_glob: Only search files matching this glob (e.g. '**/*.py'). Default '*'.
    """
    root = Path(directory).expanduser().resolve()
    if not root.exists():
        return f"Error: directory not found: {root}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex '{pattern}': {e}"

    # Collect matching files
    if "**" in file_glob or "/" in file_glob:
        candidates = root.glob(file_glob)
    else:
        candidates = root.rglob(file_glob)

    results: list[str] = []
    for filepath in sorted(candidates):
        if not filepath.is_file():
            continue
        # Skip binary files and hidden dirs
        rel = str(filepath.relative_to(root))
        if any(part.startswith(".") for part in filepath.parts if part != "."):
            continue

        try:
            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for i, line in enumerate(lines, 1):
            if regex.search(line):
                results.append(f"{rel}:{i}: {line.rstrip()}")
                if len(results) >= MAX_GREP_LINES:
                    return (
                        f"Found {MAX_GREP_LINES}+ matches (truncated):\n"
                        + "\n".join(results)
                    )

    if not results:
        return f"No matches for '{pattern}' in {root} (glob: {file_glob})"

    return f"Found {len(results)} matches:\n" + "\n".join(results)


@tool
def list_dir(directory: str = ".") -> str:
    """List the contents of a directory. Shows files and subdirectories.

    Args:
        directory: Path to the directory. Default is current dir.
    """
    root = Path(directory).expanduser().resolve()
    if not root.exists():
        return f"Error: directory not found: {root}"
    if not root.is_dir():
        return f"Error: not a directory: {root}"

    entries = sorted(root.iterdir())
    lines: list[str] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue  # Skip hidden files/dirs
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"  {entry.name}{suffix}")

    if not lines:
        return f"{root}/ (empty)"

    return f"{root}/\n" + "\n".join(lines)


@tool
def write_file(file_path: str, content: str) -> str:
    """Write content to a file. Creates parent directories if needed.

    Args:
        file_path: Path to the file to write.
        content: The content to write.
    """
    path = Path(file_path).expanduser().resolve()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


# --- Grouped exports for easy binding to agent nodes ---

# Read-only tools — safe for research and architect nodes
READ_TOOLS = [read_file, glob_files, grep_content, list_dir]

# All tools including write — for implement node (future)
WRITE_TOOLS = [read_file, glob_files, grep_content, list_dir, write_file]
