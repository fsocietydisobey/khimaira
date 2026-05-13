"""Pool router — select the cheapest competent model from the auto-mode pool.

Different from `khimaira.dispatch.router.route()`: that one routes chain-task
classifications down a fallback chain. This one operates on the auto-mode
pool (the registry's `enabled_for_auto=True` subset) and picks the cheapest
model whose capabilities cover the classification's requirements.

Inputs:
  - TaskClassification (from the classifier)
  - The registry (defaults shipped + user override at ~/.khimaira/models.yaml)
  - Which runners are actually installed on this machine

Outputs:
  - PoolDecision: the picked entry + an audit trail (pool_size,
    eligible_size, top_2 candidates, rejected_reasons). Logged alongside
    every auto-mode dispatch so mis-routes are debuggable post-hoc.

The audit fields exist because "the classifier mis-routed" stays vibes-based
without them. With them, `khimaira usage savings --audit` can show "of N
auto calls in the last 7d, M had only one eligible candidate; you'd benefit
from enabling more models."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from khimaira_types import TaskClassification

from khimaira.log import get_logger

from .registry import ModelEntry, auto_pool, load_registry
from .runners import RUNNERS

log = get_logger("dispatch.pool_router")


# Reference token counts used to score "cost" per model. We don't know
# the actual dispatch size up front, so we pick a representative
# delegate-style call: short prompt, short reply. Same reference for
# every model = a comparable ranking score.
_REF_INPUT_TOKENS = 1000
_REF_OUTPUT_TOKENS = 500


@dataclass
class PoolDecision:
    """The auto-pool router's output. Includes a full audit trail."""

    chosen: ModelEntry | None
    required_caps: frozenset[str]
    classifier_confidence: float

    # Audit fields — for `khimaira usage savings --audit` post-hoc review.
    pool_size: int  # total enabled-for-auto entries in the registry
    available_size: int  # of those, how many have an installed runner
    eligible_size: int  # of those, how many cover required_caps
    top_2: list[tuple[str, float]] = field(default_factory=list)  # (model_id, score)
    rejected: dict[str, str] = field(default_factory=dict)  # model_id → reason

    refused: bool = False
    refusal_reason: str | None = None

    def to_audit_dict(self) -> dict:
        """Compact dict shape for the routing-decision log line."""
        return {
            "chosen_id": self.chosen.id if self.chosen else None,
            "chosen_runner": self.chosen.runner if self.chosen else None,
            "required_caps": sorted(self.required_caps),
            "classifier_confidence": self.classifier_confidence,
            "pool_size": self.pool_size,
            "available_size": self.available_size,
            "eligible_size": self.eligible_size,
            "top_2": self.top_2,
            "rejected_reasons": self.rejected,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
        }


def derive_required_caps(c: TaskClassification) -> frozenset[str]:
    """Translate a TaskClassification into the capability set a model needs
    to cover before it's eligible.

    The mapping is intentionally loose — auto mode shouldn't be picky.
    The classifier already says "this is trivial" or "this needs deep
    reasoning"; the pool router just enforces that the chosen model can
    actually handle that shape of work.

    Reasoning: matching is a SUBSET test (required ⊆ available). Tight
    requirements eliminate eligible candidates and force escalation to
    pricier models. Loose requirements keep the cheap pool large.
    """
    caps: set[str] = set()

    # Task type → capability hint
    if c.task_type in ("implement", "refactor", "debug"):
        caps.add("code")
    elif c.task_type == "architect":
        caps.add("code")  # architect tasks usually touch code
        caps.add("deep-reasoning")
    elif c.task_type == "research":
        caps.add("factual")

    # Complexity escalation — only added when the task NEEDS it
    if c.complexity_tier in ("complex", "extreme"):
        caps.add("deep-reasoning")
    elif c.complexity_tier == "medium":
        caps.add("reasoning")

    # Thinking-level escalation overrides downward (high beats medium)
    if c.thinking_level == "high":
        caps.add("deep-reasoning")

    # Avoid contradictions: deep-reasoning implies reasoning;
    # if both are present, drop the weaker requirement so the subset
    # test stays clean.
    if "deep-reasoning" in caps:
        caps.discard("reasoning")

    return frozenset(caps)


def _is_runner_available(runner_name: str) -> bool:
    """Check installed-ness. Wrapped for test stubbing."""
    runner = RUNNERS.get(runner_name)
    if runner is None:
        return False
    try:
        return bool(runner.is_available())
    except Exception as e:  # noqa: BLE001 — runner probes can fail in weird ways
        log.warning("pool_router: %s.is_available() raised %s; treating as down", runner_name, e)
        return False


def _score(entry: ModelEntry) -> float:
    """Cost score for ranking. Lower = cheaper.

    Uses a fixed reference token mix (1000 in / 500 out) so every model
    gets the same yardstick. Local models score 0 → always cheapest;
    ties broken by model id alphabetically for stable ordering.
    """
    return entry.estimate_cost(_REF_INPUT_TOKENS, _REF_OUTPUT_TOKENS)


def select_from_pool(
    classification: TaskClassification,
    registry: list[ModelEntry] | None = None,
    runner_available: Callable[[str], bool] | None = None,
) -> PoolDecision:
    """Pick the cheapest model from the auto pool that covers required caps.

    Args:
        classification: from the classifier.
        registry: override registry (test injection). Default: load from disk.
        runner_available: override runner-availability check (test injection).
            Default: probe each runner's `is_available()`.

    Pipeline:
      1. Load auto_pool (enabled_for_auto=True).
      2. Drop entries whose runner isn't installed → audit `rejected[id]=runner-unavailable`.
      3. Drop entries that don't cover required_caps → audit `rejected[id]=missing-cap:X`.
      4. Sort survivors by cost (ascending); pick the cheapest.
      5. Record top_2 candidates for the audit log.
    """
    is_avail = runner_available or _is_runner_available
    reg = registry if registry is not None else load_registry()
    pool = auto_pool(reg)
    required = derive_required_caps(classification)

    rejected: dict[str, str] = {}

    # Step 1 → 2: filter on runner availability
    available_pool: list[ModelEntry] = []
    for entry in pool:
        if not is_avail(entry.runner):
            rejected[entry.id] = f"runner-unavailable:{entry.runner}"
            continue
        available_pool.append(entry)

    # Step 3: filter on capability subset
    eligible: list[ModelEntry] = []
    for entry in available_pool:
        if not entry.supports(set(required)):
            missing = required - set(entry.capabilities)
            rejected[entry.id] = f"missing-caps:{','.join(sorted(missing))}"
            continue
        eligible.append(entry)

    # Step 4: rank by cost. Tie-break by id for stable ordering.
    eligible.sort(key=lambda e: (_score(e), e.id))

    pool_size = len(pool)
    available_size = len(available_pool)
    eligible_size = len(eligible)

    if not eligible:
        # No model survived. The most informative refusal: tell the caller
        # WHICH stage everything died at.
        if pool_size == 0:
            reason = (
                "auto pool is empty — every registry entry has "
                "enabled_for_auto=false. Run `khimaira models enable <id>`."
            )
        elif available_size == 0:
            reason = (
                f"no auto-pool runner installed (pool size {pool_size}; "
                "all rejected for runner-unavailable). "
                "Install at least one of: claude, gemini, codex, ollama."
            )
        else:
            reason = (
                f"no auto-pool entry covers required capabilities "
                f"{sorted(required)} (available size {available_size}, "
                f"eligible size 0). Relax classifier requirements or "
                f"enable a more-capable model in `khimaira models`."
            )
        return PoolDecision(
            chosen=None,
            required_caps=required,
            classifier_confidence=classification.confidence,
            pool_size=pool_size,
            available_size=available_size,
            eligible_size=0,
            top_2=[],
            rejected=rejected,
            refused=True,
            refusal_reason=reason,
        )

    chosen = eligible[0]
    top_2 = [(e.id, _score(e)) for e in eligible[:2]]

    log.info(
        "pool_router: %s (runner=%s, score=$%.5f) — required=%s, eligible=%d/%d/%d",
        chosen.id,
        chosen.runner,
        _score(chosen),
        sorted(required) or "[]",
        eligible_size,
        available_size,
        pool_size,
    )

    return PoolDecision(
        chosen=chosen,
        required_caps=required,
        classifier_confidence=classification.confidence,
        pool_size=pool_size,
        available_size=available_size,
        eligible_size=eligible_size,
        top_2=top_2,
        rejected=rejected,
    )
