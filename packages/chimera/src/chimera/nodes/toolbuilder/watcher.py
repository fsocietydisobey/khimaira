"""POB watcher — observes developer behavior from public artifacts.

Reads shell history, git patterns, test timing, and file sizes to produce
behavioral signals. Purely deterministic (no LLM calls).
"""

import asyncio
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.toolbuilder_watcher")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


@dataclass
class BehaviorSignal:
    """A single observed behavior pattern."""

    signal_type: str  # repeated_command | file_churn | slow_test | large_file | frequent_git_op
    description: str
    evidence: str
    frequency: int
    impact: str  # low | medium | high


def _find_history_file() -> Path | None:
    """Find the user's shell history file."""
    for path in [
        Path.home() / ".zsh_history",
        Path.home() / ".bash_history",
        Path(os.environ.get("HISTFILE", "")),
    ]:
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def _parse_history(history_path: Path, max_lines: int = 500) -> list[BehaviorSignal]:
    """Parse shell history for repeated command patterns."""
    signals: list[BehaviorSignal] = []

    try:
        raw = history_path.read_bytes()
        # Handle zsh history format (: timestamp:0;command)
        text = raw.decode("utf-8", errors="replace")
        lines = text.strip().split("\n")[-max_lines:]

        # Clean zsh history format
        cleaned = []
        for line in lines:
            if line.startswith(": "):
                # zsh format: ": timestamp:0;command"
                match = re.match(r"^: \d+:\d+;(.+)$", line)
                if match:
                    cleaned.append(match.group(1).strip())
            else:
                cleaned.append(line.strip())

        # Count command frequencies
        counter = Counter(cleaned)
        for cmd, count in counter.most_common(20):
            if count >= 5 and len(cmd) > 3:
                impact = "high" if count >= 50 else "medium" if count >= 20 else "low"
                signals.append(BehaviorSignal(
                    signal_type="repeated_command",
                    description=f"Command typed {count} times: {cmd[:80]}",
                    evidence=cmd,
                    frequency=count,
                    impact=impact,
                ))

        # Find multi-command patterns (chained with && or ;)
        chains = [c for c in cleaned if "&&" in c or ";" in c]
        chain_counter = Counter(chains)
        for chain, count in chain_counter.most_common(5):
            if count >= 3:
                signals.append(BehaviorSignal(
                    signal_type="repeated_command",
                    description=f"Chained command typed {count} times: {chain[:80]}",
                    evidence=chain,
                    frequency=count,
                    impact="medium" if count < 20 else "high",
                ))

    except (OSError, UnicodeDecodeError) as e:
        log.warning("failed to parse history: %s", e)

    return signals


async def _analyze_git_patterns() -> list[BehaviorSignal]:
    """Analyze git log for file churn patterns."""
    signals: list[BehaviorSignal] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--stat", "--format=%H", "-50",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_PROJECT_ROOT),
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")

        # Count file change frequency
        file_changes: Counter[str] = Counter()
        for line in output.split("\n"):
            line = line.strip()
            if "|" in line and ("+" in line or "-" in line):
                file_name = line.split("|")[0].strip()
                if file_name:
                    file_changes[file_name] += 1

        # Files changed in >40% of commits = high churn
        for file_name, count in file_changes.most_common(10):
            if count >= 20:
                signals.append(BehaviorSignal(
                    signal_type="file_churn",
                    description=f"File changed in {count}/50 recent commits: {file_name}",
                    evidence=file_name,
                    frequency=count,
                    impact="high" if count >= 30 else "medium",
                ))

    except (FileNotFoundError, OSError) as e:
        log.warning("git analysis failed: %s", e)

    return signals


async def _find_large_files() -> list[BehaviorSignal]:
    """Find Python files over 500 lines."""
    signals: list[BehaviorSignal] = []
    src_dir = _PROJECT_ROOT / "src" / "chimera"

    if not src_dir.exists():
        return signals

    for py_file in src_dir.rglob("*.py"):
        try:
            lines = py_file.read_text().count("\n")
            if lines > 500:
                rel = str(py_file.relative_to(_PROJECT_ROOT))
                signals.append(BehaviorSignal(
                    signal_type="large_file",
                    description=f"Large file ({lines} lines): {rel}",
                    evidence=rel,
                    frequency=lines,
                    impact="medium" if lines < 800 else "high",
                ))
        except (OSError, UnicodeDecodeError):
            continue

    return signals


def build_toolbuilder_watcher_node():
    """Build a watcher node that observes developer behavior.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def toolbuilder_watcher_node(state: OrchestratorState) -> dict:
        """Observe behavior from shell history, git, and file system."""
        history = list(state.get("history", []))
        all_signals: list[dict] = []

        # Shell history analysis
        history_path = _find_history_file()
        if history_path:
            log.info("POB watcher: analyzing %s", history_path)
            shell_signals = _parse_history(history_path)
            for s in shell_signals:
                all_signals.append({
                    "type": s.signal_type,
                    "description": s.description,
                    "evidence": s.evidence,
                    "frequency": s.frequency,
                    "impact": s.impact,
                })

        # Git pattern analysis
        git_signals = await _analyze_git_patterns()
        for s in git_signals:
            all_signals.append({
                "type": s.signal_type,
                "description": s.description,
                "evidence": s.evidence,
                "frequency": s.frequency,
                "impact": s.impact,
            })

        # Large file detection
        file_signals = await _find_large_files()
        for s in file_signals:
            all_signals.append({
                "type": s.signal_type,
                "description": s.description,
                "evidence": s.evidence,
                "frequency": s.frequency,
                "impact": s.impact,
            })

        log.info("POB watcher: %d signals observed", len(all_signals))

        return {
            "toolbuilder_signals": all_signals,
            "history": history + [f"toolbuilder_watcher: {len(all_signals)} behavior signals observed"],
        }

    return toolbuilder_watcher_node
