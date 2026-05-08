"""Background job manager for LangGraph pipeline runs.

Jobs run the graph in asyncio tasks. MCP tools start jobs and poll status
without holding the connection open. Checkpoints persist via SQLite so
jobs survive server restarts (though running tasks don't — they must be
re-triggered).
"""

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from chimera.log import get_logger

log = get_logger("jobs")


@dataclass
class Job:
    """A background graph pipeline run."""

    job_id: str
    thread_id: str
    status: str = "running"  # running, paused, completed, failed
    progress: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


# In-memory job registry — jobs are ephemeral, checkpoints are persistent
_jobs: dict[str, Job] = {}


def _notify(title: str, message: str) -> None:
    """Send a desktop notification. Best-effort — fails silently."""
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=chimera", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # notify-send not installed


def notify_job_update(job: "Job") -> None:
    """Send a desktop notification for important job state changes."""
    if job.status == "paused":
        _notify(
            f"Job {job.job_id} — Waiting for Approval",
            "Architecture plan ready for review. Say 'approve' or 'status' in Cursor.",
        )
    elif job.status == "completed":
        elapsed = int((job.finished_at or time.time()) - job.started_at)
        _notify(
            f"Job {job.job_id} — Completed ({elapsed}s)",
            "Pipeline finished. Check results with 'status' in Cursor.",
        )
    elif job.status == "failed":
        _notify(
            f"Job {job.job_id} — Failed",
            f"Error: {job.error or 'unknown'}",
        )


def create_job(thread_id: str | None = None) -> Job:
    """Create a new job and register it."""
    job_id = str(uuid.uuid4())[:8]
    thread_id = thread_id or str(uuid.uuid4())
    job = Job(job_id=job_id, thread_id=thread_id)
    _jobs[job_id] = job
    log.info("created job %s (thread %s)", job_id, thread_id)
    return job


def get_job(job_id: str) -> Job | None:
    """Get a job by ID."""
    return _jobs.get(job_id)


def list_jobs(limit: int = 10) -> list[Job]:
    """List recent jobs, newest first."""
    return sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)[:limit]


def format_job_status(job: Job) -> str:
    """Format a job's current state as markdown."""
    elapsed = int((job.finished_at or time.time()) - job.started_at)
    parts = [
        f"**Job:** `{job.job_id}` | **Thread:** `{job.thread_id}`",
        f"**Status:** {job.status} | **Elapsed:** {elapsed}s",
    ]

    if job.progress:
        # Show last 5 progress messages
        recent = job.progress[-5:]
        parts.append("**Progress:**\n" + "\n".join(f"- {p}" for p in recent))
        if len(job.progress) > 5:
            parts.append(f"*({len(job.progress) - 5} earlier messages omitted)*")

    if job.error:
        parts.append(f"**Error:** {job.error}")

    if job.status == "paused" and job.result:
        plan = job.result.get("architecture_plan", "")
        if plan:
            parts.append(f"### Architecture Plan (review this)\n\n{plan}")
        parts.append(
            f"\n**To approve:** `approve(job_id=\"{job.job_id}\")`\n"
            f"**To reject:** `approve(job_id=\"{job.job_id}\", feedback=\"...\")`"
        )

    if job.status == "completed" and job.result:
        # Show key outputs
        plan = job.result.get("architecture_plan", "")
        impl = job.result.get("implementation_result", "")
        findings = job.result.get("research_findings", "")
        if plan:
            parts.append(f"### Architecture Plan\n\n{plan}")
        if impl:
            parts.append(f"### Implementation\n\n{impl}")
        if findings:
            parts.append(f"### Research Findings\n\n{findings}")

    return "\n\n".join(parts)
