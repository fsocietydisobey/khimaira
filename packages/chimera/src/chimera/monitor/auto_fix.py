"""Auto-fix proposals — when an anomaly is observed, route it through
chimera's chain_pipeline (research → architect → implement → review)
to produce a candidate fix. Output is a markdown report saved to
disk + an opt-in `gh pr create --draft` script the user reviews
and runs.

Critical design choice — NEVER auto-merge. The flow is:

    self-watch detects anomaly
       ↓
    user/Claude calls `monitor_auto_fix(anomaly_id)` (manual trigger)
       ↓
    chain_pipeline proposes a change-set
       ↓
    saved to ~/.local/state/chimera/auto-fix/<timestamp>.md
       ↓
    `gh pr create --draft` script also saved alongside
       ↓
    user reviews → user runs the script → draft PR opens
       ↓
    user reviews PR → user merges (or closes)

Two human-in-the-loop checkpoints (the manual trigger and the PR
review). Auto-fix without those is a footgun — code that modifies
its own deployment compounds bugs silently.

Today this is intentionally manual-trigger. Fully autonomous mode
(self-watch fires investigations automatically) is one config flag
away but requires confidence that the diagnosis quality is high
enough to justify the noise. Earn that confidence first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chimera.log import get_logger

log = get_logger("monitor.auto_fix")

_OUTPUT_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "chimera" / "auto-fix"


@dataclass
class FixProposal:
    """The output of an auto-fix run — a written investigation, a
    diff (when the agent could apply edits), and a PR script."""

    anomaly_id: str             # which anomaly we investigated
    timestamp: str              # ISO timestamp of when the proposal was generated
    investigation_md: Path      # the markdown report
    pr_script: Path | None      # `gh pr create` script (may be None if no edits)
    branch_name: str | None     # the branch the diff lives on (if any)


def is_gh_available() -> bool:
    """Check whether the GitHub CLI is installed AND authenticated."""
    if not shutil.which("gh"):
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


async def propose_fix(anomaly: dict[str, Any]) -> FixProposal:
    """Run an investigation against the anomaly and produce a draft fix
    proposal. Does NOT actually open the PR — saves a script the user
    runs manually after reviewing the proposal.

    Architecture:
      1. Format the anomaly + recent context as a chimera-pipeline task
      2. Run chain_pipeline (the SPR-4 4-phase pipeline) with the task
      3. The pipeline's research/architect/implement phases produce
         analysis + a code change-set (or analysis-only when the bug
         is in interpretation, not implementation)
      4. Save the report + a PR script for the user to review

    The user retains full control. They read the markdown, decide
    whether to run the PR script, and review the resulting draft PR
    before merging.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    anomaly_id = (
        f"{anomaly.get('check', 'unknown')}_"
        f"{anomaly.get('project', 'all')}_{timestamp}"
    )

    investigation_path = _OUTPUT_DIR / f"{anomaly_id}.md"
    pr_script_path = _OUTPUT_DIR / f"{anomaly_id}.sh"

    # Format the task that gets handed to chain_pipeline
    task = _format_pipeline_task(anomaly)

    # Save the task as the investigation skeleton — chain_pipeline
    # output gets appended on completion. Write upfront so even a
    # crashed run leaves a useful trace.
    investigation_path.write_text(
        f"# Auto-fix proposal: {anomaly.get('check')}\n\n"
        f"**Generated:** {now.isoformat()}\n"
        f"**Status:** investigation in progress\n\n"
        f"## Anomaly\n\n```json\n{json.dumps(anomaly, indent=2)}\n```\n\n"
        f"## Pipeline task\n\n{task}\n\n"
        f"## Pipeline output\n\n_(running...)_\n",
        encoding="utf-8",
    )

    # Fire chain_pipeline. Lazy import to keep auto_fix.py importable
    # even when chimera-orchestration deps aren't installed.
    from chimera.config.loader import load_config
    from chimera.graphs.pipeline import build_pipeline_graph

    log.info("auto_fix: kicking off chain_pipeline for anomaly %s", anomaly_id)
    config = load_config()
    graph, _checkpointer = await build_pipeline_graph(config)
    graph_config = {"configurable": {"thread_id": f"auto-fix-{anomaly_id}"}}

    pipeline_output: list[str] = []
    try:
        async for update in graph.astream(
            {"task": task},
            config=graph_config,
            stream_mode="updates",
        ):
            if not update:
                continue
            for node_name, state_update in update.items():
                if node_name == "__interrupt__":
                    continue
                pipeline_output.append(f"### {node_name}\n")
                if isinstance(state_update, dict):
                    # Surface the most useful fields — implementation_result,
                    # arbitration_decision, history. Skip noisy state.
                    for k in ("implementation_result", "arbitration_decision",
                              "research_findings", "architecture_plan",
                              "validation_score", "history"):
                        if k in state_update:
                            v = state_update[k]
                            pipeline_output.append(f"**{k}:** {_short_repr(v)}\n")
                pipeline_output.append("\n")
    except Exception as exc:
        pipeline_output.append(f"### error\n\n`{exc!r}`\n")
        log.warning("auto_fix: chain_pipeline failed: %s", exc)

    # Append pipeline output to the investigation markdown
    body = investigation_path.read_text(encoding="utf-8")
    body = body.replace("_(running...)_\n", "".join(pipeline_output) + "\n")
    investigation_path.write_text(body, encoding="utf-8")

    # Build a PR script the user can review + run. We don't auto-execute.
    branch_name = f"auto-fix/{anomaly_id}"
    pr_script_path.write_text(
        _build_pr_script(branch_name, investigation_path, anomaly),
        encoding="utf-8",
    )
    pr_script_path.chmod(0o755)

    log.info(
        "auto_fix: proposal saved → %s; review then run %s",
        investigation_path,
        pr_script_path,
    )
    return FixProposal(
        anomaly_id=anomaly_id,
        timestamp=now.isoformat(),
        investigation_md=investigation_path,
        pr_script=pr_script_path,
        branch_name=branch_name,
    )


def _format_pipeline_task(anomaly: dict[str, Any]) -> str:
    """Build the chain_pipeline task description from an anomaly."""
    return (
        f"chimera-monitor self-watch detected an anomaly. Investigate "
        f"the root cause and propose a fix.\n\n"
        f"## Anomaly\n"
        f"- check: {anomaly.get('check')}\n"
        f"- project: {anomaly.get('project')}\n"
        f"- severity: {anomaly.get('severity')}\n"
        f"- detail: {anomaly.get('detail')}\n"
        f"- evidence: {json.dumps(anomaly.get('evidence', {}))}\n\n"
        f"## What to do\n"
        f"1. Identify the root cause (likely a bug in chimera-monitor "
        f"itself, or a misconfigured project, or a real upstream issue).\n"
        f"2. Propose a code change that fixes the root cause WITHOUT\n"
        f"   masking the symptom (don't just suppress the anomaly check).\n"
        f"3. If a code fix is appropriate, write it. If the anomaly is\n"
        f"   informational only, document why and recommend whether to\n"
        f"   tighten or loosen the check.\n\n"
        f"## Constraints\n"
        f"- Modify ONLY chimera's own source (under src/chimera/).\n"
        f"- Add a regression test (tests/) for any code change.\n"
        f"- Do not touch deployment, CI, or any path outside this repo.\n"
        f"- Output is a draft PR for human review, not a merge."
    )


def _short_repr(v: Any, max_len: int = 200) -> str:
    s = repr(v)
    if len(s) > max_len:
        s = s[:max_len - 3] + "..."
    return s


def _build_pr_script(branch_name: str, investigation_md: Path, anomaly: dict[str, Any]) -> str:
    """The script the user runs to actually open the draft PR.

    Kept as a script (not executed automatically) so the user
    re-reads the proposal first, decides whether the diff is right,
    optionally edits, and only then runs it.
    """
    title = f"auto-fix: {anomaly.get('check')} on {anomaly.get('project') or 'monitor'}"
    return f"""#!/bin/bash
# Draft PR for an auto-fix proposal. Review the investigation
# (\\\\`{investigation_md.name}\\\\`) before running this — chimera does NOT
# auto-merge, but it doesn't auto-validate the diff either.
#
# What this does:
#   1. Creates branch {branch_name} from current HEAD
#   2. Stages + commits any uncommitted changes (the chain_pipeline's edits)
#   3. Pushes to origin
#   4. Opens a DRAFT PR with the investigation as the body
#
# What it does NOT do:
#   - Run tests (do that yourself before pushing)
#   - Merge the PR (only ever a draft — you merge after review)

set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not installed — install from https://cli.github.com or"
  echo "open the PR manually using the investigation file as the body."
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Uncommitted changes detected. Review with \\\\`git diff\\\\`,"
  echo "then run \\\\`git checkout -b {branch_name}\\\\` and \\\\`git commit\\\\` manually,"
  echo "then re-run this script (it'll skip the branch creation if it exists)."
fi

# Branch creation — idempotent
if ! git rev-parse --verify {branch_name} >/dev/null 2>&1; then
  git checkout -b {branch_name}
else
  git checkout {branch_name}
fi

# Push + open PR
git push -u origin {branch_name}
gh pr create --draft \\\\
  --title "{title}" \\\\
  --body-file "{investigation_md}"

echo
echo "Draft PR opened. Review at the URL above."
echo "Investigation: {investigation_md}"
"""
