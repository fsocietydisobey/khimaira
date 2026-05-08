"""POB PR creator — opens a pull request with evidence and usage.

Uses the GitHub CLI (gh) to create a PR. Falls back to just pushing
the branch if gh is not available.
"""

import asyncio
from pathlib import Path

from chimera.core.state import OrchestratorState
from chimera.core.toolbuilder_memory import record_outcome
from chimera.log import get_logger

log = get_logger("node.toolbuilder_pr_creator")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


async def _run_cmd(*args: str) -> tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
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


def build_toolbuilder_pr_creator_node():
    """Build a PR creator node that opens PRs for built tools.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def toolbuilder_pr_creator_node(state: OrchestratorState) -> dict:
        """Create a PR for the forged tool."""
        history = list(state.get("history", []))
        forge_result = state.get("toolbuilder_forge_result")
        spec = state.get("toolbuilder_tool_spec")

        if not forge_result or forge_result.get("status") != "success" or not spec:
            return {
                "toolbuilder_pr_result": None,
                "history": history + ["toolbuilder_pr_creator: no successful forge — skipping"],
            }

        branch = forge_result["branch"]
        tool_name = forge_result["tool_name"]
        files = forge_result.get("files_created", [])

        # Push the branch
        log.info("POB PR: pushing branch %s", branch)
        code, stdout, stderr = await _run_cmd("git", "push", "-u", "origin", branch)
        if code != 0:
            log.warning("POB PR: push failed — %s (may not have remote)", stderr)
            # Record the proposal even without PR
            await record_outcome(
                tool_name=tool_name,
                friction_type=spec.get("category", "unknown"),
                description=spec.get("description", ""),
                accepted=True,
            )
            return {
                "toolbuilder_pr_result": {"status": "push_failed", "branch": branch, "error": stderr},
                "history": history + [f"toolbuilder_pr_creator: push failed — {stderr[:60]}"],
            }

        # Build PR body
        pr_body = (
            f"## POB observed something\n\n"
            f"**Friction observed:** {spec.get('friction_addressed', '')}\n\n"
            f"**Evidence:** {spec.get('evidence', '')}\n\n"
            f"**Tool built:** `{tool_name}` — {spec.get('description', '')}\n\n"
            f"**Estimated time saved:** {spec.get('estimated_time_saved', 'unknown')}\n\n"
            f"**Usage:**\n```\n{spec.get('usage', '')}\n```\n\n"
            f"**Files:**\n" + "\n".join(f"- `{f}`" for f in files) + "\n\n"
            "---\n"
            "*Built by POB (Proactive Observation Builder). Reject this PR if you don't want this tool.*"
        )

        # Try to create PR via gh
        code, stdout, stderr = await _run_cmd(
            "gh", "pr", "create",
            "--title", f"pob: add {tool_name}",
            "--body", pr_body,
            "--head", branch,
        )

        if code == 0:
            pr_url = stdout.strip()
            log.info("POB PR: created — %s", pr_url)
            await record_outcome(
                tool_name=tool_name,
                friction_type=spec.get("category", "unknown"),
                description=spec.get("description", ""),
                accepted=True,
            )
            return {
                "toolbuilder_pr_result": {"status": "created", "url": pr_url, "branch": branch},
                "implementation_result": (
                    f"POB created PR: {pr_url}\n\nTool: {tool_name}\n{spec.get('description', '')}"
                ),
                "history": history + [f"toolbuilder_pr_creator: PR created — {pr_url}"],
            }
        else:
            log.warning("POB PR: gh pr create failed — %s", stderr)
            await record_outcome(
                tool_name=tool_name,
                friction_type=spec.get("category", "unknown"),
                description=spec.get("description", ""),
                accepted=True,
            )
            return {
                "toolbuilder_pr_result": {"status": "branch_only", "branch": branch, "error": stderr},
                "implementation_result": (
                    f"POB built tool on branch `{branch}`.\nGH CLI unavailable — create PR manually."
                ),
                "history": history + [f"toolbuilder_pr_creator: branch pushed, PR creation failed — {stderr[:60]}"],
            }

    return toolbuilder_pr_creator_node
