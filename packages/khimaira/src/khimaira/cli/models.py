"""`khimaira models` — inspect and sync the model registry.

The registry has two layers: the shipped default at
`khimaira/dispatch/default_models.yaml` (released with each khimaira
version) and the user's overrides at `~/.khimaira/models.yaml` (or
`$XDG_CONFIG_HOME/khimaira/models.yaml`).

Today (pre-#57) the user has no way to see when the shipped defaults
have drifted relative to their local overrides. When Anthropic /
Google / OpenAI release a new model, or change pricing, the shipped
defaults capture it on the next khimaira release — but the user's
override file needs human reconciliation.

`khimaira models sync` shows the diff:
  - models the shipped defaults have that the user doesn't
  - models the user has that the shipped defaults don't
  - models both have but with different fields (cost / capabilities /
    enabled_for_auto)

Default mode is read-only — prints the diff. `--apply` overwrites the
user file with the shipped defaults, BUT preserves any entries whose
id only exists in the user file (i.e. user-added models stay).

`khimaira models list` is the read-only view of the merged registry
(what auto-mode actually sees).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from khimaira.dispatch.registry import (
    ModelEntry,
    _parse_entry,
    _user_registry_path,
    load_registry,
)
from khimaira.log import get_logger

log = get_logger("cli.models")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "models",
        help="Inspect or sync the model registry.",
        description=(
            "Show what's in the merged model registry (shipped defaults + "
            "user overrides), or sync the user file against the shipped "
            "defaults to pick up new releases / pricing updates."
        ),
    )
    sub = p.add_subparsers(dest="models_subcommand", required=True)

    _add_list_subparser(sub)
    _add_sync_subparser(sub)


def _add_list_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="Show the merged registry (what auto-mode actually sees).",
    )
    p.add_argument(
        "--enabled-only",
        action="store_true",
        help="Only show models with enabled_for_auto: true.",
    )
    p.set_defaults(func=_run_list)


def _add_sync_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sync",
        help="Diff user registry against shipped defaults; optionally apply.",
        description=(
            "Read-only by default — prints what would change. Pass --apply to "
            "write the updated registry to ~/.khimaira/models.yaml. "
            "User-added models (entries whose id is NOT in the shipped "
            "defaults) are preserved across --apply."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write the updated registry. Without this, sync only prints the diff.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when --apply is set.",
    )
    p.set_defaults(func=_run_sync)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _run_list(args: argparse.Namespace) -> int:
    models = load_registry()
    if args.enabled_only:
        models = [m for m in models if m.enabled_for_auto]

    if not models:
        print("(no models registered — check khimaira/dispatch/default_models.yaml)")
        return 1

    # Group by runner
    by_runner: dict[str, list[ModelEntry]] = {}
    for m in models:
        by_runner.setdefault(m.runner, []).append(m)

    for runner_name in sorted(by_runner):
        print(f"\n{runner_name}:")
        for m in sorted(by_runner[runner_name], key=lambda x: x.id):
            tags = ", ".join(m.capabilities) if m.capabilities else "(no tags)"
            status = "auto" if m.enabled_for_auto else "OFF"
            print(
                f"  {m.id:<32s}  ${m.cost_per_1m.input:>5.2f}/M in  "
                f"${m.cost_per_1m.output:>5.2f}/M out  [{status:<3s}]  {tags}"
            )

    return 0


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@dataclass
class _RegistryDiff:
    """Three-way breakdown of shipped vs user model registries."""

    added_in_default: list[ModelEntry] = field(default_factory=list)
    removed_in_default: list[ModelEntry] = field(default_factory=list)
    changed: list[tuple[ModelEntry, ModelEntry]] = field(default_factory=list)
    user_only: list[ModelEntry] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(self.added_in_default or self.removed_in_default or self.changed)


def _load_default_models() -> dict[str, ModelEntry]:
    """Load the shipped default model registry. Keys are model ids."""
    pkg = resources.files("khimaira.dispatch")
    text = (pkg / "default_models.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    entries: dict[str, ModelEntry] = {}
    for raw in data.get("models", []):
        try:
            entry = _parse_entry(raw)
            entries[entry.id] = entry
        except ValueError as e:
            log.warning("models sync: skipping malformed shipped entry: %s", e)
    return entries


def _load_user_models(path: Path) -> dict[str, ModelEntry]:
    """Load the user's override registry. Empty dict if the file
    doesn't exist or is empty."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(
            f"[khimaira models sync] {path} is malformed YAML: {e}\n"
            "Refusing to sync — fix or remove the file first.",
            file=sys.stderr,
        )
        raise SystemExit(3) from e
    entries: dict[str, ModelEntry] = {}
    for raw in data.get("models", []) or []:
        try:
            entry = _parse_entry(raw)
            entries[entry.id] = entry
        except ValueError as e:
            log.warning("models sync: skipping malformed user entry: %s", e)
    return entries


def _diff_registries(
    shipped: dict[str, ModelEntry],
    user: dict[str, ModelEntry],
) -> _RegistryDiff:
    diff = _RegistryDiff()
    for model_id, shipped_entry in shipped.items():
        if model_id not in user:
            diff.added_in_default.append(shipped_entry)
            continue
        user_entry = user[model_id]
        if shipped_entry != user_entry:
            diff.changed.append((user_entry, shipped_entry))
    for model_id, user_entry in user.items():
        if model_id not in shipped:
            diff.user_only.append(user_entry)
    return diff


def _print_diff(diff: _RegistryDiff) -> None:
    if not diff.has_changes() and not diff.user_only:
        print("✅ user registry matches shipped defaults; no sync needed.")
        return

    if diff.added_in_default:
        print(f"\nNew in shipped defaults ({len(diff.added_in_default)}):")
        for m in diff.added_in_default:
            print(f"  + {m.id} ({m.runner})  ${m.cost_per_1m.input}/M in")

    if diff.changed:
        print(f"\nChanged in shipped defaults ({len(diff.changed)}):")
        for user_e, shipped_e in diff.changed:
            print(f"  ~ {shipped_e.id}")
            if user_e.cost_per_1m != shipped_e.cost_per_1m:
                print(
                    f"      cost: ${user_e.cost_per_1m.input}/${user_e.cost_per_1m.output} "
                    f"→ ${shipped_e.cost_per_1m.input}/${shipped_e.cost_per_1m.output}"
                )
            if user_e.capabilities != shipped_e.capabilities:
                added = set(shipped_e.capabilities) - set(user_e.capabilities)
                removed = set(user_e.capabilities) - set(shipped_e.capabilities)
                if added:
                    print(f"      +caps: {', '.join(sorted(added))}")
                if removed:
                    print(f"      -caps: {', '.join(sorted(removed))}")
            if user_e.enabled_for_auto != shipped_e.enabled_for_auto:
                print(
                    f"      enabled_for_auto: {user_e.enabled_for_auto} → "
                    f"{shipped_e.enabled_for_auto}"
                )

    if diff.user_only:
        print(
            f"\nKept (user-only, NOT touched by sync, {len(diff.user_only)}):"
        )
        for m in diff.user_only:
            print(f"  = {m.id} ({m.runner})")


def _serialize_registry(entries: list[ModelEntry]) -> str:
    """Render a list of ModelEntry as the canonical YAML format that
    `_parse_entry` round-trips against."""
    out: list[dict[str, Any]] = []
    for m in sorted(entries, key=lambda x: (x.runner, x.id)):
        d: dict[str, Any] = {
            "id": m.id,
            "runner": m.runner,
        }
        if m.capabilities:
            d["capabilities"] = list(m.capabilities)
        if m.cost_per_1m.input != 0.0 or m.cost_per_1m.output != 0.0:
            d["cost_per_1m"] = {
                "input": m.cost_per_1m.input,
                "output": m.cost_per_1m.output,
            }
        if m.subscription != "unknown":
            d["subscription"] = m.subscription
        if not m.enabled_for_auto:
            d["enabled_for_auto"] = False
        out.append(d)
    return yaml.safe_dump(
        {"models": out},
        sort_keys=False,
        default_flow_style=False,
    )


def _run_sync(args: argparse.Namespace) -> int:
    shipped = _load_default_models()
    user_path = _user_registry_path()
    user = _load_user_models(user_path)
    diff = _diff_registries(shipped, user)

    _print_diff(diff)

    if not diff.has_changes():
        return 0

    if not args.apply:
        print("\nRun `khimaira models sync --apply` to write these changes.")
        return 0

    if not args.yes:
        try:
            answer = input(
                f"\nApply to {user_path}? (y/N) "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Build the new registry: shipped defaults + user-only entries preserved.
    merged: list[ModelEntry] = list(shipped.values()) + diff.user_only

    # Backup the existing file
    if user_path.is_file():
        backup = user_path.with_suffix(f".yaml.bak.{int(user_path.stat().st_mtime)}")
        shutil.copy2(user_path, backup)
        print(f"[khimaira models sync] backup: {backup}")

    # Atomic write
    user_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = user_path.with_suffix(".yaml.tmp")
    tmp.write_text(_serialize_registry(merged), encoding="utf-8")
    tmp.replace(user_path)

    print(f"[khimaira models sync] wrote {user_path} ({len(merged)} models)")
    return 0
