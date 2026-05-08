"""Git safety tools for CLR (refinement loop).

Provides checkpoint, revert, and diff functions. All operations use
asyncio subprocess calls with cwd set to the project root.
"""

import asyncio
from pathlib import Path

from chimera.log import get_logger

log = get_logger("git_tools")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


async def _run_git(*args: str) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_PROJECT_ROOT),
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def git_checkpoint(message: str) -> bool:
    """Stage all changes and commit. Returns True if commit succeeded."""
    # Check if there are changes to commit
    code, stdout, _ = await _run_git("status", "--porcelain")
    if not stdout.strip():
        log.info("nothing to commit")
        return False

    await _run_git("add", "-A")
    code, stdout, stderr = await _run_git("commit", "-m", f"clr: {message}")
    if code == 0:
        log.info("committed: %s", message)
        return True
    else:
        log.warning("commit failed: %s", stderr)
        return False


async def git_revert() -> bool:
    """Revert the last commit. Returns True if revert succeeded."""
    code, stdout, stderr = await _run_git("revert", "HEAD", "--no-edit")
    if code == 0:
        log.info("reverted HEAD")
        return True
    else:
        log.warning("revert failed: %s", stderr)
        return False


async def git_diff_files() -> list[str]:
    """Return list of files changed in the last commit."""
    code, stdout, _ = await _run_git("diff", "--name-only", "HEAD~1", "HEAD")
    if code == 0 and stdout:
        return [f for f in stdout.split("\n") if f.strip()]
    return []


async def is_self_modification(changed_files: list[str] | None = None) -> bool:
    """Check if any orchestrator source files were modified."""
    files = changed_files or await git_diff_files()
    return any(f.startswith("src/orchestrator/") for f in files)
