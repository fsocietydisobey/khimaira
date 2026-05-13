"""`khimaira task` — context-resolved, auto-routed dispatch of a dev task.

The end-to-end pipeline:

  user task description
      ↓
  [classifier] (cheap CLI runner)  →  TaskClassification
      ↓
  [router]                         →  RoutingDecision
      ↓
  [dispatch] (chosen CLI runner)   →  RunnerResult
      ↓
  [usage tracker]                  →  appended to usage.jsonl
      ↓
  print result + cost summary

Phase 4 (context resolver — Séance + Scarlet) plugs in BETWEEN the user
input and the classifier; this version skips it (no resolver yet) so the
runner just sees the raw task text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from khimaira.dispatch.classifier import classify_task
from khimaira.dispatch.router import route
from khimaira.dispatch.runners import get_runner
from khimaira.log import get_logger
from khimaira.usage import get_recorder, runner_to_provider

log = get_logger("cli.task")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `khimaira task` on the main argparse parser."""
    p = subparsers.add_parser(
        "task",
        help="Run a dev task — auto-routed across your installed AI CLIs.",
        description=(
            "Classifies the task with a cheap model, routes to the cheapest "
            "competent runner (your terminal AI subscription, local Ollama, "
            "or `llm`-wrapped provider), and prints the result + cost summary."
        ),
    )
    p.add_argument("description", help="What you want done.")
    p.add_argument(
        "--project",
        default=None,
        help="Project directory for per-project routing-table overrides. Default: $PWD.",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Max USD this task is allowed to cost. Refuses dispatch if classifier estimates over.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run classifier + router; print the decision without dispatching.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override the dispatched runner's timeout (seconds).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Synchronous entry — argparse hands off here, we drive asyncio inside."""
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    from khimaira.dispatch.runners.claude import ClaudeAuthError

    project = args.project or "."

    # Step 1 — classify
    print(f"[khimaira task] classifying...", file=sys.stderr)
    t0 = time.monotonic()
    try:
        classification = await classify_task(args.description, project_path=project)
    except ClaudeAuthError as e:
        print(
            f"\n[khimaira task] STOP — Claude rejected the call:\n  {e}\n"
            "Skipping retries to avoid burning more quota. "
            "Run `khimaira doctor` to check available alternatives.",
            file=sys.stderr,
        )
        return 5
    except Exception as e:
        print(f"\n[khimaira task] classifier failed: {e}", file=sys.stderr)
        return 1
    print(
        f"[khimaira task] {classification.task_type}/{classification.complexity_tier} "
        f"(confidence {classification.confidence:.0%}) — {classification.reasoning}",
        file=sys.stderr,
    )

    # Step 2 — route
    decision = route(
        classification,
        project_path=project,
        budget_remaining_usd=args.budget,
    )

    if decision.refused:
        print(f"\n[khimaira task] REFUSED: {decision.refusal_reason}", file=sys.stderr)
        return 2

    if decision.fallback_reason:
        print(f"[khimaira task] note: {decision.fallback_reason}", file=sys.stderr)

    print(
        f"[khimaira task] dispatch → {decision.chosen_runner}"
        f"{('/' + decision.chosen_model) if decision.chosen_model else ''}"
        f"{(' thinking=' + str(decision.chosen_thinking_budget_tokens)) if decision.chosen_thinking_budget_tokens else ''}",
        file=sys.stderr,
    )

    if args.dry_run:
        print("\n[khimaira task] dry-run — not dispatching", file=sys.stderr)
        return 0

    # Step 3 — dispatch
    runner = get_runner(decision.chosen_runner)
    if not runner.is_available():
        print(
            f"\n[khimaira task] runner {decision.chosen_runner} not available "
            "(should not happen — router checked). Aborting.",
            file=sys.stderr,
        )
        return 3

    runner_kwargs: dict[str, object] = {}
    if decision.chosen_thinking_budget_tokens > 0 and decision.chosen_runner == "claude":
        # Claude expresses thinking budget via --effort tier rather than tokens.
        # Simple mapping: 0=low, 1-2k=medium, 2-8k=high. (Future: per-runner kwarg adapters.)
        if decision.chosen_thinking_budget_tokens >= 2048:
            runner_kwargs["effort"] = "high"
        else:
            runner_kwargs["effort"] = "medium"

    try:
        result = await runner.run(
            args.description,
            model=decision.chosen_model or None,
            timeout=args.timeout,
            cwd=args.project,
            **runner_kwargs,
        )
    except ClaudeAuthError as e:
        print(
            f"\n[khimaira task] STOP — Claude rejected the dispatch:\n  {e}\nNot retrying.",
            file=sys.stderr,
        )
        return 5
    except Exception as e:
        print(f"\n[khimaira task] runner {decision.chosen_runner} failed: {e}", file=sys.stderr)
        return 4

    elapsed = time.monotonic() - t0

    # Step 4 — record usage
    await get_recorder().record(
        runner=decision.chosen_runner,
        provider=runner_to_provider(decision.chosen_runner),
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_s=result.latency_s,
        role=classification.task_type,
        task_id=decision.task_id,
        source="cli",
        mode="manual",
    )

    # Step 5 — print result + cost summary
    print(result.text)

    from khimaira.usage import estimate_cost

    cost = estimate_cost(result.model, result.input_tokens, result.output_tokens)
    print(
        f"\n[khimaira task] done in {elapsed:.1f}s · "
        f"{result.input_tokens} in / {result.output_tokens} out · "
        f"~${cost:.4f}",
        file=sys.stderr,
    )
    return 0
