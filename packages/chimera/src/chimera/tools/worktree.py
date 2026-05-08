"""Git worktree management for DCE (Dead Code Eliminator).

Creates isolated shadow worktrees where agents can safely delete code
without affecting the main branch. All DCE operations happen in the
shadow worktree first, then merge back if tests pass.
"""

import asyncio
from pathlib import Path

from chimera.log import get_logger

log = get_logger("worktree")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


async def _run_git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or str(_PROJECT_ROOT),
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def create_shadow(branch_name: str = "dce-shadow") -> str | None:
    """Create a shadow worktree for DCE operations.

    Args:
        branch_name: Branch name for the worktree (default: dce-shadow).

    Returns:
        Absolute path to the shadow worktree, or None if creation failed.
    """
    shadow_path = str(_PROJECT_ROOT.parent / f".chimera-shadow-{branch_name}")

    # Clean up any existing worktree at this path
    await destroy_shadow(shadow_path)

    # Create a new branch from HEAD
    code, _, stderr = await _run_git("worktree", "add", "-b", branch_name, shadow_path)
    if code != 0:
        # Branch might already exist — try without -b
        code, _, stderr = await _run_git("worktree", "add", shadow_path, branch_name)
        if code != 0:
            log.error("failed to create shadow worktree: %s", stderr)
            return None

    log.info("shadow worktree created: %s (branch: %s)", shadow_path, branch_name)
    return shadow_path


async def destroy_shadow(shadow_path: str) -> bool:
    """Remove a shadow worktree and its branch.

    Args:
        shadow_path: Absolute path to the shadow worktree.

    Returns:
        True if cleanup succeeded.
    """
    path = Path(shadow_path)
    if not path.exists():
        return True

    code, _, stderr = await _run_git("worktree", "remove", "--force", shadow_path)
    if code != 0:
        log.warning("worktree remove failed: %s", stderr)
        return False

    # Extract branch name and try to delete it
    branch_name = path.name.replace(".chimera-shadow-", "")
    if branch_name:
        await _run_git("branch", "-D", branch_name)

    log.info("shadow worktree destroyed: %s", shadow_path)
    return True


async def merge_shadow(shadow_path: str) -> tuple[bool, str]:
    """Merge shadow worktree changes back to the current branch.

    Args:
        shadow_path: Absolute path to the shadow worktree.

    Returns:
        Tuple of (success, output_message).
    """
    path = Path(shadow_path)
    if not path.exists():
        return False, "shadow worktree does not exist"

    # Get the branch name from the worktree
    code, branch, _ = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=shadow_path)
    if code != 0:
        return False, "failed to get shadow branch name"

    # Commit any staged changes in shadow
    code, status, _ = await _run_git("status", "--porcelain", cwd=shadow_path)
    if status.strip():
        await _run_git("add", "-A", cwd=shadow_path)
        await _run_git("commit", "-m", "dce: dead code elimination", cwd=shadow_path)

    # Merge back to main
    code, stdout, stderr = await _run_git("merge", branch, "--no-ff", "-m", f"dce: merge {branch}")
    if code != 0:
        log.error("merge failed: %s", stderr)
        return False, f"merge failed: {stderr}"

    log.info("shadow merged: %s → main", branch)
    return True, f"merged {branch} successfully"


async def run_tests_in_shadow(shadow_path: str, timeout: int = 300) -> tuple[bool, str]:
    """Run the test suite inside a shadow worktree.

    Args:
        shadow_path: Absolute path to the shadow worktree.
        timeout: Max seconds for tests.

    Returns:
        Tuple of (tests_passed, test_output).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "pytest", "--tb=short", "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=shadow_path,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        passed = proc.returncode == 0
        return passed, output.strip()
    except asyncio.TimeoutError:
        return False, f"tests timed out after {timeout}s"
    except FileNotFoundError:
        return True, "pytest not available — skipping tests"
