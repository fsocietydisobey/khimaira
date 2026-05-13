"""`khimaira route <description>` — classify-only.

Useful for dry-run / debug / "what would khimaira do?" inspection.
Doesn't dispatch — just runs the classifier and router and prints
the decision as JSON.

Cheap (only the classifier runs, ~$0.0004 on Haiku-tier or $0 on Ollama).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from khimaira.dispatch.classifier import classify_task
from khimaira.dispatch.router import route


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "route",
        help="Classify-only: print the routing decision without dispatching.",
        description=(
            "Runs the classifier + router and prints the JSON decision. "
            "Useful for inspecting what khimaira WOULD do. "
            "Doesn't burn the dispatched runner — only the classifier itself."
        ),
    )
    p.add_argument("description", help="Task description.")
    p.add_argument("--project", default=None)
    p.add_argument("--budget", type=float, default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    classification = await classify_task(args.description, project_path=args.project or ".")
    decision = route(
        classification,
        project_path=args.project or ".",
        budget_remaining_usd=args.budget,
    )
    print(json.dumps(decision.model_dump(), indent=2, default=str))
    return 0 if not decision.refused else 2
