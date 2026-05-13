"""Model registry — the source of truth for auto-mode's selectable pool.

A model registry is a list of every LLM the user wants khimaira to
consider for AUTO routing. Each entry pairs a model name with:

  - which CLI runner can dispatch it (claude / codex / gemini / ollama / llm)
  - capability tags (factual, code, reasoning, vision, fast, cheap, …)
  - cost per million tokens (input + output) for savings calculations
  - subscription source (which paid plan it counts against)
  - `enabled_for_auto` toggle — user can exclude any model from the
    auto pool without removing it entirely (still callable via explicit
    tier/model arg)

Load order:
  1. Shipped defaults (khimaira/dispatch/default_models.yaml)
  2. ~/.khimaira/models.yaml (user overrides — adds, removes, edits)

User overrides MERGE by `id`: a registry entry with the same id replaces
the shipped default for that model. Set `enabled_for_auto: false` in the
override to disable a shipped model without losing its capability tags
(so explicit calls still work).

Used by:
  - khimaira.dispatch.router for auto-mode model selection
  - mcp__khimaira__delegate for tier resolution
  - khimaira usage savings for cost calculations
  - khimaira models {list,sync,add,enable,disable} CLI surface
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from khimaira.log import get_logger

log = get_logger("dispatch.registry")


@dataclass(frozen=True)
class ModelCost:
    """Cost per million tokens (input, output). Used for savings math.

    Rates are public-list-price estimates — not exact billing. Anthropic /
    Google / OpenAI all publish these; ollama is $0 (local). When prices
    change, users (or khimaira releases) update the registry.
    """

    input: float
    output: float


@dataclass
class ModelEntry:
    """One model in the registry. Identity = `id` + `runner`."""

    id: str
    runner: str
    capabilities: tuple[str, ...] = ()
    cost_per_1m: ModelCost = field(default_factory=lambda: ModelCost(0.0, 0.0))
    subscription: str = "unknown"
    enabled_for_auto: bool = True

    def supports(self, required: set[str]) -> bool:
        """True iff this model has every required capability tag."""
        return required.issubset(self.capabilities)

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Compute estimated USD cost for a dispatch with these token counts."""
        return (
            input_tokens * self.cost_per_1m.input + output_tokens * self.cost_per_1m.output
        ) / 1_000_000.0


def _user_registry_path() -> Path:
    """Per-user override path. Honors XDG_CONFIG_HOME, falls back to ~/.khimaira/."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "khimaira" / "models.yaml"
    return Path(os.path.expanduser("~/.khimaira/models.yaml"))


def _parse_entry(raw: dict[str, Any]) -> ModelEntry:
    """Validate + construct one ModelEntry from a YAML mapping."""
    if not isinstance(raw, dict):
        raise ValueError(f"model entry must be a mapping, got {type(raw).__name__}")
    if "id" not in raw or "runner" not in raw:
        raise ValueError(f"model entry needs id + runner; got {raw!r}")

    cost_raw = raw.get("cost_per_1m") or {}
    if not isinstance(cost_raw, dict):
        raise ValueError(f"cost_per_1m must be a mapping; got {cost_raw!r}")
    cost = ModelCost(
        input=float(cost_raw.get("input", 0.0)),
        output=float(cost_raw.get("output", 0.0)),
    )

    caps_raw = raw.get("capabilities") or ()
    if not isinstance(caps_raw, (list, tuple)):
        raise ValueError(f"capabilities must be a list; got {caps_raw!r}")
    capabilities = tuple(str(c) for c in caps_raw)

    return ModelEntry(
        id=str(raw["id"]),
        runner=str(raw["runner"]),
        capabilities=capabilities,
        cost_per_1m=cost,
        subscription=str(raw.get("subscription") or "unknown"),
        enabled_for_auto=bool(raw.get("enabled_for_auto", True)),
    )


def _load_yaml(path: Path) -> list[ModelEntry]:
    """Read a registry YAML file. Returns [] on missing/empty/malformed —
    never raises (registry loading is hot path; misformatted user file
    shouldn't break dispatch).
    """
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("registry: failed to read %s: %s", path, e)
        return []
    if not isinstance(raw, dict):
        log.warning("registry: %s top-level isn't a mapping", path)
        return []
    entries_raw = raw.get("models") or []
    out: list[ModelEntry] = []
    for r in entries_raw:
        try:
            out.append(_parse_entry(r))
        except ValueError as e:
            log.warning("registry: skipping invalid entry in %s: %s", path, e)
    return out


def _load_shipped() -> list[ModelEntry]:
    """Read the khimaira-shipped default registry from package data."""
    try:
        pkg = resources.files("khimaira.dispatch")
        text = (pkg / "default_models.yaml").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        log.warning("registry: shipped default_models.yaml is malformed: %s", e)
        return []
    entries_raw = raw.get("models") or []
    out: list[ModelEntry] = []
    for r in entries_raw:
        try:
            out.append(_parse_entry(r))
        except ValueError as e:
            log.warning("registry: skipping invalid shipped entry: %s", e)
    return out


def load_registry() -> list[ModelEntry]:
    """Load shipped defaults + merge user overrides.

    Merge rule: a user entry with the same `id` replaces the shipped
    default entirely. This means users can fully redefine a model's
    capabilities / cost / enabled-state with one override, or disable it
    by adding `enabled_for_auto: false` to the override.

    Order in the returned list: user-only entries first, then merged/
    shipped. Order doesn't affect routing (router sorts by cost), but
    it's stable for `khimaira models list` output.
    """
    shipped = _load_shipped()
    user = _load_yaml(_user_registry_path())

    user_ids = {e.id for e in user}
    merged = list(user) + [e for e in shipped if e.id not in user_ids]
    return merged


def auto_pool(registry: list[ModelEntry] | None = None) -> list[ModelEntry]:
    """The subset of the registry eligible for auto-mode routing.

    Filters out models with `enabled_for_auto=False`. Caller still has
    to filter by which runners are actually installed — that's a
    runtime check (see khimaira.dispatch.runners.available_runners).
    """
    reg = registry if registry is not None else load_registry()
    return [m for m in reg if m.enabled_for_auto]


def find_by_id(model_id: str, registry: list[ModelEntry] | None = None) -> ModelEntry | None:
    """Look up a single model by id. Used by usage tracking + savings math
    to find cost data for a dispatched model."""
    reg = registry if registry is not None else load_registry()
    for m in reg:
        if m.id == model_id:
            return m
    return None
