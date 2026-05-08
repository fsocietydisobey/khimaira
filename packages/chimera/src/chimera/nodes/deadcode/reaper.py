"""DCE reaper — deletes dead code in risk-ordered batches.

Processes targets from the seeker in order: safe → moderate → risky.
After each batch, runs tests. If tests fail, reverts that batch.
Archives all deletions to the DCE archive.
"""

import asyncio
from pathlib import Path

from chimera.core.deadcode_archive import archive_deletion
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.tools.worktree import run_tests_in_shadow

log = get_logger("node.deadcode_reaper")


async def _run_git(*args: str, cwd: str) -> tuple[int, str, str]:
    """Run a git command in the specified directory."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _delete_target(target: dict, shadow_path: str) -> tuple[bool, int]:
    """Delete a single dead code target. Returns (success, lines_removed)."""
    file_path = Path(shadow_path) / target["file"]
    if not file_path.exists():
        return False, 0

    target_type = target.get("type", "")

    if target_type == "unused_import":
        # Remove the specific import line
        line_num = target.get("line", 0)
        if line_num <= 0:
            return False, 0

        lines = file_path.read_text().splitlines(keepends=True)
        if line_num <= len(lines):
            lines[line_num - 1] = ""
            file_path.write_text("".join(lines))
            return True, 1

    elif target_type == "unused_variable":
        line_num = target.get("line", 0)
        if line_num <= 0:
            return False, 0

        lines = file_path.read_text().splitlines(keepends=True)
        if line_num <= len(lines):
            lines[line_num - 1] = ""
            file_path.write_text("".join(lines))
            return True, 1

    elif target_type in ("unreferenced_function", "unreferenced_class"):
        # More cautious — just log for now, don't auto-delete
        # These need human review since AST analysis can miss dynamic usage
        return False, 0

    return False, 0


def build_deadcode_reaper_node():
    """Build a reaper node that deletes dead code in batches.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def deadcode_reaper_node(state: OrchestratorState) -> dict:
        """Delete dead code targets in risk-ordered batches."""
        history = list(state.get("history", []))
        shadow_path = state.get("deadcode_shadow_path", "")
        targets_map = state.get("deadcode_targets") or {}
        targets = targets_map.get("targets", [])

        if not targets or not shadow_path:
            return {
                "deadcode_reap_result": {"deleted": 0, "reverted": 0, "skipped": 0, "lines_removed": 0},
                "history": history + ["deadcode_reaper: no targets to delete"],
            }

        deleted = 0
        reverted = 0
        skipped = 0
        total_lines_removed = 0

        # Process by risk level: safe → moderate (risky requires human review)
        for risk_level in ("safe", "moderate"):
            batch = [t for t in targets if t.get("risk") == risk_level]
            if not batch:
                continue

            log.info("DCE reaper: processing %d %s targets", len(batch), risk_level)

            # Checkpoint before this batch
            await _run_git("add", "-A", cwd=shadow_path)
            await _run_git("commit", "-m", f"dce: pre-{risk_level}-batch checkpoint",
                           "--allow-empty", cwd=shadow_path)

            batch_deleted = 0
            batch_lines = 0
            for target in batch:
                success, lines = await _delete_target(target, shadow_path)
                if success:
                    batch_deleted += 1
                    batch_lines += lines
                    # Archive the deletion
                    await archive_deletion(
                        path=target["file"],
                        name=target.get("name", "unknown"),
                        reason=target.get("evidence", "dead code"),
                        category=target.get("type", "unknown"),
                        lines_removed=lines,
                        risk_level=risk_level,
                    )
                else:
                    skipped += 1

            if batch_deleted == 0:
                continue

            # Test after batch
            log.info("DCE reaper: testing after %d deletions (%s)", batch_deleted, risk_level)
            tests_passed, test_output = await run_tests_in_shadow(shadow_path)

            if tests_passed:
                # Commit the batch
                await _run_git("add", "-A", cwd=shadow_path)
                await _run_git("commit", "-m",
                               f"dce: removed {batch_deleted} {risk_level} targets ({batch_lines} lines)",
                               cwd=shadow_path)
                deleted += batch_deleted
                total_lines_removed += batch_lines
                log.info("DCE reaper: %s batch committed (%d targets, %d lines)",
                         risk_level, batch_deleted, batch_lines)
            else:
                # Revert the batch
                await _run_git("checkout", ".", cwd=shadow_path)
                reverted += batch_deleted
                log.warning("DCE reaper: %s batch reverted — tests failed", risk_level)

        # Count risky targets as skipped (need human review)
        risky_count = sum(1 for t in targets if t.get("risk") == "risky")
        skipped += risky_count

        result = {
            "deleted": deleted,
            "reverted": reverted,
            "skipped": skipped,
            "lines_removed": total_lines_removed,
            "risky_pending_review": risky_count,
        }

        log.info(
            "DCE reaper: deleted=%d, reverted=%d, skipped=%d, lines=%d",
            deleted, reverted, skipped, total_lines_removed,
        )

        return {
            "deadcode_reap_result": result,
            "history": history + [
                f"deadcode_reaper: deleted {deleted}, reverted {reverted}, "
                f"skipped {skipped}, lines removed {total_lines_removed}"
            ],
        }

    return deadcode_reaper_node
