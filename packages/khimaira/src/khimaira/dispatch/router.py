"""AMR router — turns TaskClassification into RoutingDecision.

The classifier *recommends* a runner+model. The router *picks* the actual
dispatch given:
  - what's installed on this machine
  - whether the user opted into local-only mode
  - per-task budget remaining (when caller passed one)
  - the routing table (shipped defaults + user/project overrides)

Output is a RoutingDecision with both the recommendation AND the actual
choice — so the dashboard can display "AMR wanted Ollama but used Claude
because Ollama wasn't running."
"""

from __future__ import annotations

import uuid

from khimaira_types import RoutingDecision, TaskClassification

from khimaira.config import is_local_only_mode, load_routing_table
from khimaira.log import get_logger

from .runners import RUNNERS

log = get_logger("dispatch.router")


def route(
    classification: TaskClassification,
    *,
    project_path: str | None = None,
    task_id: str | None = None,
    budget_remaining_usd: float | None = None,
) -> RoutingDecision:
    """Pick the actual runner+model. Apply availability + privacy + budget gates.

    Returns a RoutingDecision. If `refused=True`, the caller should NOT
    dispatch — instead surface `refusal_reason` to the user.
    """
    config = load_routing_table(project_path)
    fallback_chains = config.get("fallback_chains", {})
    local_only = config.get("local_only_runners", ["ollama", "llm"])
    privacy_mode = is_local_only_mode()

    task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"

    recommended_runner = classification.recommended_runner
    recommended_model = classification.recommended_model

    # Budget gate FIRST — if the recommended dispatch exceeds remaining
    # budget, refuse. Don't try to find a cheaper alternative; the
    # classifier already ran with budget context, this is a hard stop.
    if (
        budget_remaining_usd is not None
        and classification.estimated_cost_usd_max > budget_remaining_usd
    ):
        return RoutingDecision(
            classification=classification,
            chosen_runner=recommended_runner,
            chosen_model=recommended_model,
            chosen_thinking_budget_tokens=classification.thinking_budget_tokens,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            refused=True,
            refusal_reason=(
                f"Estimated cost ${classification.estimated_cost_usd_max:.4f} "
                f"exceeds remaining budget ${budget_remaining_usd:.4f} "
                f"for task {task_id}."
            ),
        )

    # Privacy gate — when KHIMAIRA_LOCAL_ONLY=1, only local runners are eligible
    if privacy_mode and recommended_runner not in local_only:
        log.info(
            "router: privacy mode enabled — overriding %s with local runner",
            recommended_runner,
        )
        # Walk local-only list for first available
        for candidate in local_only:
            if RUNNERS.get(candidate) and RUNNERS[candidate].is_available():
                return RoutingDecision(
                    classification=classification,
                    chosen_runner=candidate,
                    chosen_model="",  # use runner default — overrides aren't valid for a different provider
                    chosen_thinking_budget_tokens=0,
                    fallback_reason=(
                        f"KHIMAIRA_LOCAL_ONLY=1; overrode "
                        f"{recommended_runner}/{recommended_model} → {candidate}"
                    ),
                    task_id=task_id,
                    budget_remaining_usd=budget_remaining_usd,
                )
        return RoutingDecision(
            classification=classification,
            chosen_runner=recommended_runner,
            chosen_model=recommended_model,
            chosen_thinking_budget_tokens=classification.thinking_budget_tokens,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            refused=True,
            refusal_reason=(
                "KHIMAIRA_LOCAL_ONLY=1 but no local runner is available. "
                "Install Ollama or llm CLI."
            ),
        )

    # Availability gate — if recommended runner isn't installed, walk the
    # fallback chain. The first available runner wins.
    chain = fallback_chains.get(
        recommended_runner,
        [recommended_runner, "claude", "ollama", "llm"],
    )

    for candidate in chain:
        runner = RUNNERS.get(candidate)
        if runner is None:
            continue
        if not runner.is_available():
            continue

        if candidate == recommended_runner:
            # Happy path — recommendation matches what's installed
            return RoutingDecision(
                classification=classification,
                chosen_runner=candidate,
                chosen_model=recommended_model,
                chosen_thinking_budget_tokens=classification.thinking_budget_tokens,
                task_id=task_id,
                budget_remaining_usd=budget_remaining_usd,
            )

        # Fallback path — recommendation isn't installed, candidate is.
        # We DON'T carry the original model identifier across runners
        # (model names are runner-specific) — let the fallback runner
        # use its own default.
        return RoutingDecision(
            classification=classification,
            chosen_runner=candidate,
            chosen_model="",  # runner default
            chosen_thinking_budget_tokens=0,
            fallback_reason=(
                f"Recommended runner {recommended_runner!r} not available; "
                f"using {candidate!r} from fallback chain."
            ),
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
        )

    # Nothing in the chain is available — refuse
    return RoutingDecision(
        classification=classification,
        chosen_runner=recommended_runner,
        chosen_model=recommended_model,
        chosen_thinking_budget_tokens=classification.thinking_budget_tokens,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        refused=True,
        refusal_reason=(
            f"No runner from fallback chain is available: {chain}. "
            "Run `khimaira doctor` to diagnose."
        ),
    )
