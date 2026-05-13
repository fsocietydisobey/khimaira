"""AMR classifier — cheap-runner call that produces TaskClassification.

The first step of the auto-router pipeline. Takes a task description, asks
a cheap model to categorize it, returns a TaskClassification that the
router then turns into a concrete dispatch.

Cost model: classifier runs at ~$0.0004/call on Haiku-tier (or $0 on
Ollama). The savings from routing trivial tasks down-tier dwarf the
classifier's own bill.

This module deliberately keeps the prompt + invocation logic very small so
the cost stays predictable. Heavy reasoning belongs to the actual task
runner, not the classifier.
"""

from __future__ import annotations

import time

from khimaira_types import TaskClassification

from khimaira.config import load_routing_table
from khimaira.log import get_logger

from .runners import RUNNERS, get_runner
from .structured import StructuredCallError, run_structured

log = get_logger("dispatch.classifier")


_CLASSIFIER_PROMPT = """You are the khimaira AMR (Automatic Model Router) classifier.

Your job: read the dev task below and emit a routing recommendation.

Task:
\"\"\"
{task}
\"\"\"

{context_block}

Classification rubric:
- task_type: which kind of work is this?
    - research:  understanding existing code, finding things, exploration
    - architect: design decisions, system layout, multi-file plans
    - implement: write new code from scratch
    - refactor:  rewrite existing code (rename, restructure, modernize)
    - debug:     find + fix a specific bug
    - classify:  short categorical decision, taxonomy assignment
    - format:    mechanical formatting / cleanup
    - explain:   describe code behavior in prose
    - chat:      conversational, no code change expected
    - other:     doesn't fit above

- complexity_tier: how much capability is needed?
    - trivial: rename, format, single-line, anything mechanical
    - simple:  single-file change ≤ 30 lines, modest reasoning
    - medium:  1-3 file change, real reasoning needed
    - complex: multi-file, architectural understanding required
    - extreme: whole-system, deep cross-cutting

- thinking_level: extended-reasoning budget needed?
    - none:   one-shot — no reasoning trace required
    - low:    brief reasoning helps
    - medium: substantive reasoning needed
    - high:   deep multi-step planning

- recommended_runner + recommended_model: pick from these available runners:
{available_runners_block}

- estimated_cost_usd_max: worst-case for this dispatch (be generous; this is a ceiling).
- reasoning: 1 sentence — why these choices?
- confidence: 0.0–1.0 — how sure are you of the classification?

Be conservative on routing: when in doubt, prefer the cheaper-tier option.
Devs can always escalate manually. Burning subscription quota on trivial
tasks is the failure mode khimaira exists to prevent."""


async def classify_task(
    task: str,
    *,
    project_path: str | None = None,
    context_summary: str | None = None,
) -> TaskClassification:
    """Run the classifier and return a TaskClassification.

    Args:
        task: User's task description (the original prompt).
        project_path: Used to load per-project routing-table overrides.
        context_summary: Optional pre-resolved context summary the
            classifier can use to refine its complexity estimate
            (e.g., "3 files / 240 lines pre-resolved by context resolver").
    """
    config = load_routing_table(project_path)
    classifier_cfg = config.get("classifier", {})

    preferred = classifier_cfg.get("preferred_runner", "ollama")
    preferred_model = classifier_cfg.get("preferred_model", "llama3.3:70b")
    fallback = classifier_cfg.get("fallback_runner", "claude")
    fallback_model = classifier_cfg.get("fallback_model", "claude-haiku-4-5")
    timeout = int(classifier_cfg.get("timeout_s", 30))

    # Pick a runner that's actually installed
    chosen_runner: str
    chosen_model: str
    if RUNNERS[preferred].is_available():
        chosen_runner, chosen_model = preferred, preferred_model
    elif RUNNERS[fallback].is_available():
        chosen_runner, chosen_model = fallback, fallback_model
        log.info(
            "classifier: preferred runner %s unavailable; using fallback %s",
            preferred, fallback,
        )
    else:
        # Last resort — find ANY available runner
        for name, runner in RUNNERS.items():
            if runner.is_available():
                chosen_runner, chosen_model = name, ""  # use runner default
                log.warning(
                    "classifier: neither preferred nor fallback available; "
                    "using last-resort runner %s",
                    name,
                )
                break
        else:
            raise RuntimeError(
                "classifier: no runner is available. Install at least one of: "
                "Claude Code, Codex CLI, Gemini CLI, Ollama, llm. "
                "Run `khimaira doctor` to diagnose."
            )

    available_block = _format_available_runners()
    context_block = (
        f"Context already resolved:\n{context_summary}\n"
        if context_summary
        else "No pre-resolved context."
    )

    prompt = _CLASSIFIER_PROMPT.format(
        task=task,
        context_block=context_block,
        available_runners_block=available_block,
    )

    log.info("classifier: classifying via %s/%s", chosen_runner, chosen_model)
    t0 = time.monotonic()

    try:
        classification, _result = await run_structured(
            chosen_runner,
            prompt,
            TaskClassification,
            model=chosen_model or None,
            timeout=timeout,
            max_retries=2,
        )
    except StructuredCallError as e:
        # Fallback: synthesize a conservative classification rather than fail
        # the whole dispatch. Better to over-route to a capable runner than
        # to refuse.
        log.warning(
            "classifier: structured call failed (%d attempts); "
            "synthesizing conservative default — %s",
            e.attempts, e,
        )
        return _conservative_fallback(task)

    elapsed = time.monotonic() - t0
    log.info(
        "classifier: %s/%s → runner=%s model=%s confidence=%.2f (%.1fs)",
        classification.task_type, classification.complexity_tier,
        classification.recommended_runner, classification.recommended_model,
        classification.confidence, elapsed,
    )
    return classification


def _format_available_runners() -> str:
    """Build the bullet list the classifier prompt uses."""
    lines = []
    for name, runner in RUNNERS.items():
        marker = "✅" if runner.is_available() else "❌"
        lines.append(f"  {marker} {name}")
    return "\n".join(lines)


def _conservative_fallback(task: str) -> TaskClassification:
    """When the classifier itself fails, default to a safe-but-not-cheap
    routing: medium-complexity research-type task on Claude Sonnet. Better
    to overpay slightly than to refuse work."""
    return TaskClassification(
        task_type="other",
        complexity_tier="medium",
        thinking_level="low",
        recommended_runner="claude",
        recommended_model="claude-sonnet-4-6",
        thinking_budget_tokens=0,
        estimated_cost_usd_max=0.10,
        reasoning=(
            "Classifier failed to produce a valid response after retries. "
            "Defaulting to medium-tier on Claude Sonnet to avoid refusing work. "
            "Task: " + task[:200]
        ),
        confidence=0.3,
        forced_by="classifier_fallback",
    )
