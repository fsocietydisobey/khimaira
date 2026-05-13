"""POB forge — builds the tool in a git branch.

Creates a branch, uses Claude CLI to write the tool based on the spec,
and validates the output. Never touches product code.
"""

import asyncio
from pathlib import Path

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_claude
from khimaira.core.state import OrchestratorState
from khimaira.log import get_logger

log = get_logger("node.toolbuilder_forge")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent

ALLOWED_DIRS = {"scripts/", "tools/", ".github/"}
FORBIDDEN_PATHS = {"DIRECTIVES.md", "SPEC.md", "src/"}


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


def build_toolbuilder_forge_node():
    """Build a forge node that creates tools from specs.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def toolbuilder_forge_node(state: OrchestratorState) -> dict:
        """Build the tool on a branch using Claude CLI."""
        history = list(state.get("history", []))
        spec = state.get("toolbuilder_tool_spec")

        if not spec:
            return {
                "toolbuilder_forge_result": None,
                "history": history + ["toolbuilder_forge: no tool spec — skipping"],
            }

        tool_name = spec.get("name", "unknown-tool")
        branch_name = f"pob/{tool_name}"

        # Create branch
        log.info("POB forge: creating branch %s", branch_name)
        code, _, stderr = await _run_git("checkout", "-b", branch_name)
        if code != 0:
            # Branch might exist — try switching
            code, _, stderr = await _run_git("checkout", branch_name)
            if code != 0:
                log.error("POB forge: failed to create branch — %s", stderr)
                return {
                    "toolbuilder_forge_result": {"status": "failed", "error": f"branch creation failed: {stderr}"},
                    "history": history + [f"toolbuilder_forge: failed to create branch {branch_name}"],
                }

        # Ensure target directories exist
        for file_path in spec.get("files_to_create", []):
            target_dir = _PROJECT_ROOT / Path(file_path).parent
            target_dir.mkdir(parents=True, exist_ok=True)

        # Build the tool via Claude CLI
        files_list = "\n".join(f"- {f}" for f in spec.get("files_to_create", []))
        prompt = build_prompt(
            "You are a tool builder. Create the specified tool files. "
            "Write ONLY the files listed. Do NOT modify any existing files. "
            "Do NOT touch anything under src/.",
            f"## Tool Specification\n\n"
            f"**Name:** {tool_name}\n"
            f"**Description:** {spec.get('description', '')}\n"
            f"**Friction addressed:** {spec.get('friction_addressed', '')}\n"
            f"**Usage:** {spec.get('usage', '')}\n\n"
            f"## Files to create\n\n{files_list}",
        )

        try:
            result = await run_claude(prompt, timeout=300, permission_mode="acceptEdits")
            log.info("POB forge: Claude CLI completed — %d chars output", len(result))
        except Exception as e:
            log.error("POB forge: Claude CLI failed — %s", e)
            # Switch back to main
            await _run_git("checkout", "-")
            return {
                "toolbuilder_forge_result": {"status": "failed", "error": str(e)},
                "history": history + [f"toolbuilder_forge: build failed — {e}"],
            }

        # Validate: check that only allowed files were created/modified
        code, diff_output, _ = await _run_git("diff", "--name-only")
        code2, untracked, _ = await _run_git("ls-files", "--others", "--exclude-standard")
        all_changed = (diff_output + "\n" + untracked).strip().split("\n")
        all_changed = [f for f in all_changed if f.strip()]

        violations = [
            f for f in all_changed
            if not any(f.startswith(d) for d in ALLOWED_DIRS)
        ]

        if violations:
            log.warning("POB forge: %d forbidden file changes — reverting", len(violations))
            await _run_git("checkout", ".")
            await _run_git("checkout", "-")
            return {
                "toolbuilder_forge_result": {
                    "status": "failed",
                    "error": f"forbidden files modified: {violations}",
                },
                "history": history + [f"toolbuilder_forge: rejected — touched forbidden files: {violations}"],
            }

        # Commit the tool
        await _run_git("add", "-A")
        code, _, stderr = await _run_git(
            "commit", "-m", f"pob: add {tool_name} — {spec.get('description', '')[:60]}"
        )

        forge_result = {
            "status": "success",
            "branch": branch_name,
            "files_created": [f for f in all_changed if f.strip()],
            "tool_name": tool_name,
        }

        # Switch back to main
        await _run_git("checkout", "-")

        log.info("POB forge: tool %s built on branch %s", tool_name, branch_name)

        return {
            "toolbuilder_forge_result": forge_result,
            "history": history + [f"toolbuilder_forge: {tool_name} built on {branch_name}"],
        }

    return toolbuilder_forge_node
