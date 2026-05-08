"""Task decomposer — task decomposition for PDE parallel dispatch.

Decomposes a high-level goal into N independent, file-disjoint tasks.
Supports two dispatch modes: flat (all at once) and PDE-F
(graduated generations based on dependencies).
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.task_decomposer")

DECOMPOSER_SYSTEM_PROMPT = """\
You are a task decomposer for a parallel execution system. Given a goal,
break it into independent sub-tasks that can be executed simultaneously.

## Rules

1. Each task MUST specify which files it will modify (the "files" field).
2. No two tasks may share the same file. If two changes touch the same file,
   merge them into one task.
3. Each task must be self-contained — an agent should be able to complete it
   without knowing about the other tasks.
4. Estimate complexity: "trivial" (< 5 min), "simple" (5-15 min), "moderate" (15-30 min).
5. If a task depends on another task's output, set the dependencies field.
   Tasks with no dependencies can run in parallel immediately.
6. Keep tasks focused — one clear change per task.
7. Maximum 15 tasks. If the goal requires more, focus on the most impactful.

## Dispatch mode

- If ALL tasks are independent (empty dependencies) → dispatch_mode: "flat"
- If some tasks depend on others → dispatch_mode: "pdef"
  (the system will dispatch in graduated generations: 1 → 1 → 2 → 3 → 5)
"""


class SwarmTask(BaseModel):
    """A single parallelizable task."""

    id: str = Field(description="Short kebab-case ID (e.g. 'fix-auth-types')")
    description: str = Field(description="What to do — be specific")
    files: list[str] = Field(description="Files this task will modify (exclusive ownership)")
    estimated_complexity: str = Field(description="trivial | simple | moderate")
    dependencies: list[str] = Field(
        default_factory=list,
        description="IDs of tasks that must complete before this one. Empty = independent.",
    )


class TaskManifest(BaseModel):
    """Structured decomposition from the task decomposer."""

    tasks: list[SwarmTask] = Field(description="Independent tasks (max 15)")
    dispatch_mode: str = Field(description="flat | pdef")
    reasoning: str = Field(description="Why this decomposition was chosen")


def build_swarm_decomposer_node(model: BaseChatModel):
    """Build a task decomposition node.

    Args:
        model: LangChain chat model (Sonnet recommended for quality decomposition).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(TaskManifest)

    async def task_decomposer_node(state: OrchestratorState) -> dict:
        """Decompose the goal into parallel tasks."""
        task = state.get("task", "")
        context = state.get("context", "")
        history = list(state.get("history", []))
        budget = state.get("swarm_budget") or {}

        max_agents = budget.get("max_agents", 10)

        prompt_parts = [f"## Goal\n\n{task}"]
        if context:
            prompt_parts.append(f"## Context\n\n{context}")
        prompt_parts.append(f"## Budget\n\nMaximum {max_agents} parallel tasks.")

        messages = [
            SystemMessage(content=DECOMPOSER_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]

        manifest = await structured_model.ainvoke(messages)
        assert isinstance(manifest, TaskManifest)

        # Validate file ownership — no two tasks share a file
        file_owners: dict[str, str] = {}
        conflicts: list[str] = []
        for t in manifest.tasks:
            for f in t.files:
                if f in file_owners:
                    conflicts.append(f"{f} claimed by both {file_owners[f]} and {t.id}")
                else:
                    file_owners[f] = t.id

        if conflicts:
            log.warning("file ownership conflicts: %s", conflicts)

        # Cap at budget
        tasks = manifest.tasks[:max_agents]

        log.info(
            "decomposed into %d tasks (mode=%s): %s",
            len(tasks), manifest.dispatch_mode,
            ", ".join(t.id for t in tasks),
        )

        manifest_dict = {
            "tasks": [t.model_dump() for t in tasks],
            "dispatch_mode": manifest.dispatch_mode,
            "reasoning": manifest.reasoning,
        }

        return {
            "swarm_manifest": manifest_dict,
            "dispatch_mode": manifest.dispatch_mode,
            "history": history + [
                f"decomposer: decomposed into {len(tasks)} tasks "
                f"(mode={manifest.dispatch_mode}) — {', '.join(t.id for t in tasks)}"
            ],
        }

    return task_decomposer_node


# --- PDE-F (graduated dispatch) utilities ---

def _pdef_sequence(n: int) -> list[int]:
    """First n PDE-F sequence numbers (1, 1, 2, 3, 5, 8...)."""
    if n == 0:
        return []
    seq = [1, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq[:n]


def pdef_budget(generation: int, base_tokens: int = 2000) -> int:
    """Token budget per agent in a generation, scaled by PDE-F sequence sequence."""
    seq = _pdef_sequence(generation + 1)
    return base_tokens * seq[-1] if seq else base_tokens


def sort_into_generations(tasks: list[dict]) -> list[list[dict]]:
    """Group tasks into PDE-F-width generations by dependency depth.

    Tasks with no dependencies go in Gen 1. Tasks that depend on Gen 1
    tasks go in Gen 2. And so on. Each generation's width is capped to
    the PDE-F sequence (1, 1, 2, 3, 5, 8...).

    Args:
        tasks: List of task dicts with 'id' and 'dependencies' fields.

    Returns:
        List of generations, each a list of task dicts.
    """
    if not tasks:
        return []

    # Build dependency depth via BFS from roots
    task_map = {t["id"]: t for t in tasks}
    depth: dict[str, int] = {}

    # Initialize roots (no dependencies)
    for t in tasks:
        deps = t.get("dependencies", [])
        if not deps:
            depth[t["id"]] = 0

    # Propagate depths
    changed = True
    iterations = 0
    while changed and iterations < 100:
        changed = False
        iterations += 1
        for t in tasks:
            tid = t["id"]
            deps = t.get("dependencies", [])
            if tid in depth:
                continue
            # All dependencies resolved?
            dep_depths = [depth.get(d) for d in deps]
            if all(dd is not None for dd in dep_depths):
                depth[tid] = max(dd for dd in dep_depths if dd is not None) + 1
                changed = True

    # Tasks with unresolved dependencies go in the last generation
    max_depth = max(depth.values(), default=0)
    for t in tasks:
        if t["id"] not in depth:
            depth[t["id"]] = max_depth + 1

    # Group by depth
    max_depth = max(depth.values(), default=0)
    generations: list[list[dict]] = []
    for d in range(max_depth + 1):
        gen = [t for t in tasks if depth.get(t["id"]) == d]
        if gen:
            generations.append(gen)

    # Cap widths to PDE-F sequence
    fib = _pdef_sequence(len(generations))
    for i, gen in enumerate(generations):
        cap = fib[i] if i < len(fib) else fib[-1]
        if len(gen) > cap:
            # Overflow tasks stay in this generation but will be dispatched sequentially
            log.info("gen %d: %d tasks exceeds PDE-F cap %d", i + 1, len(gen), cap)

    return generations
