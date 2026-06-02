"""Data model for Themis rule definitions and evaluation results.

Core types:
  Matcher        — one tool-match criterion (exact name or tool_input_field regex)
  Invariant      — a rule: id, severity, matchers (OR-combined), optional conditions
  RuleSet        — parsed collection of invariants for one role, with source-layer
                   annotations for debuggability
  EvalResult     — outcome of evaluate(): ok=True (pass) or ok=False + violation detail
  ViolationRecord — a logged violation entry (written to JSONL by violations.py)

YAML is loaded via load_rules(role) which resolves and merges the extends-chain:
  <instance>.yaml → <role>.base.yaml → universal.base.yaml (all optional)

Inheritance (D2):
  - An instance file may declare `extends: <base-name>` (no suffix → loads <base-name>.yaml
    from the same rules dir).
  - Merge is BY INVARIANT ID: universal → base → instance; highest layer wins by id.
  - Instance may ADD (new id), OVERRIDE (same id), or `disable: [id]` for base-DEFAULT rules.
  - LOCKED rules (`locked: true` in a base file) are IMMUTABLE to instances — an attempt
    to override or disable a locked id is SILENTLY IGNORED + LOUDLY WARNED at load time.
    The locked rule always stands (fail-safe). CI lint separately rejects locked-override
    attempts at build time (belt-and-suspenders).
  - No caching: every load_rules() call re-reads and re-merges from disk. A base-file
    change is live on the next call without any cache-invalidation step.

See VALID_ROLES for the set of assignable roles (*.base.yaml files are excluded).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

# Bundled rules directory, co-located with this package.
_RULES_DIR = Path(__file__).parent / "rules"

_log = logging.getLogger(__name__)

# Assignable roles = yaml files EXCLUDING *.base.yaml (bases are not session roles).
VALID_ROLES: frozenset[str] = frozenset(
    p.stem
    for p in _RULES_DIR.glob("*.yaml")
    if not p.stem.endswith(".base")
)


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    AUDIT = "audit"

    @property
    def rank(self) -> int:
        """Higher rank = higher priority. block wins over warn wins over audit."""
        return {"block": 3, "warn": 2, "audit": 1}[self.value]


@dataclass
class ToolInputMatcher:
    """Matches when a specific field of tool_input satisfies a regex pattern."""

    field: str
    pattern: str

    def matches(self, tool_input: dict[str, Any]) -> bool:
        value = tool_input.get(self.field)
        if value is None:
            return False
        return bool(re.search(self.pattern, str(value)))


@dataclass
class Matcher:
    """One match criterion within an invariant. OR-combined with sibling matchers."""

    tool: str
    tool_input_field: ToolInputMatcher | None = None

    def matches(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name != self.tool:
            return False
        if self.tool_input_field is not None:
            return self.tool_input_field.matches(tool_input)
        return True


@dataclass
class Invariant:
    """A single rule binding a role. Fires when ANY matcher matches AND ALL conditions pass."""

    id: str
    name: str
    severity: Severity
    matchers: list[Matcher]
    message: str
    conditions: list[str] = field(default_factory=list)
    # locked=True: this rule is a security-invariant from a base file.
    # Instances CANNOT override, disable, or weaken it (the escalation guard).
    # Semantics: the only severities are BLOCK/WARN/AUDIT (no ALLOW type exists),
    # so there is no severity-based override vector — locked guards the id itself.
    locked: bool = False
    # source_layer: the yaml stem this rule was loaded from (for debuggability).
    source_layer: str = ""

    def tool_matches(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """True if at least one matcher matches (OR-combined)."""
        return any(m.matches(tool_name, tool_input) for m in self.matchers)


@dataclass
class RuleSet:
    """Merged invariants for one role (resolved via the extends-chain).

    The `invariants` list is the FINAL merged set; each entry carries
    `source_layer` so callers can see which yaml layer contributed it.
    """

    role: str
    invariants: list[Invariant]

    @property
    def all_tool_names(self) -> list[str]:
        """Union of all tool names referenced by any invariant."""
        names: list[str] = []
        for inv in self.invariants:
            for m in inv.matchers:
                if m.tool not in names:
                    names.append(m.tool)
        return names


@dataclass
class EvalResult:
    """Result of a single themis_check call."""

    ok: bool
    role: str | None = None
    violation: ViolationDetail | None = None


@dataclass
class ViolationDetail:
    """Inline violation data returned by evaluate() and daemon endpoint."""

    rule_id: str
    name: str
    severity: Severity
    message: str


@dataclass
class ViolationRecord:
    """One entry written to themis_violations.jsonl."""

    ts: str
    session_id: str
    session_name: str
    role: str
    rule_id: str
    tool_name: str
    tool_use_id: str
    tool_input_summary: str
    decision: str  # "blocked" | "warned" | "audited"
    cwd: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "session_id": self.session_id,
            "session_name": self.session_name,
            "role": self.role,
            "rule_id": self.rule_id,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "tool_input_summary": self.tool_input_summary,
            "decision": self.decision,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ViolationRecord:
        return cls(
            ts=d["ts"],
            session_id=d["session_id"],
            session_name=d.get("session_name", ""),
            role=d.get("role", ""),
            rule_id=d["rule_id"],
            tool_name=d["tool_name"],
            tool_use_id=d.get("tool_use_id", ""),
            tool_input_summary=d.get("tool_input_summary", ""),
            decision=d["decision"],
            cwd=d.get("cwd", ""),
        )


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _parse_matcher(raw: dict[str, Any]) -> Matcher:
    tool = raw["tool"]
    tif = raw.get("tool_input_field")
    if tif:
        return Matcher(
            tool=tool,
            tool_input_field=ToolInputMatcher(field=tif["field"], pattern=tif["pattern"]),
        )
    return Matcher(tool=tool)


def _parse_invariant(raw: dict[str, Any], *, source_layer: str = "") -> Invariant:
    required = {"id", "name", "severity", "matchers", "message"}
    missing = required - set(raw)
    if missing:
        raise ValueError(f"Invariant missing required fields: {missing!r} in {raw!r}")
    severity = Severity(raw["severity"])
    matchers = [_parse_matcher(m) for m in raw["matchers"]]
    conditions = raw.get("conditions", [])
    # conditions may be a list of dicts with {check: name} or bare strings
    parsed_conditions: list[str] = []
    for c in conditions:
        if isinstance(c, dict):
            parsed_conditions.append(c["check"])
        else:
            parsed_conditions.append(str(c))
    return Invariant(
        id=raw["id"],
        name=raw["name"],
        severity=severity,
        matchers=matchers,
        message=raw["message"],
        conditions=parsed_conditions,
        locked=bool(raw.get("locked", False)),
        source_layer=source_layer,
    )


def _load_doc(name: str) -> dict[str, Any]:
    """Load a raw YAML doc by name (stem, no .yaml suffix) from the rules dir."""
    path = _RULES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No rule file for role {name!r}: {path}")
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"Rule file {path} must be a YAML mapping at the top level")
    return doc


def _resolve_extends_chain(start_name: str) -> list[tuple[str, dict[str, Any]]]:
    """Resolve the extends-chain for `start_name`, returning [(layer_name, doc), ...].

    Order: universal.base (if present) → <role>.base (if present) → instance.
    The caller walks this list from left to right, with higher layers overriding.

    Raises ValueError on cycles.
    """
    chain: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    current = start_name
    while True:
        if current in seen:
            raise ValueError(
                f"Cycle detected in Themis extends-chain for {start_name!r}: "
                f"already visited {current!r}. Chain so far: {[n for n, _ in chain]}"
            )
        seen.add(current)
        doc = _load_doc(current)
        chain.append((current, doc))
        parent = doc.get("extends")
        if parent is None:
            break
        current = str(parent)
    # chain is [instance, base, universal] — reverse to get universal→base→instance.
    chain.reverse()
    return chain


def _merge_chain(
    role: str, chain: list[tuple[str, dict[str, Any]]]
) -> list[Invariant]:
    """Merge invariants from the extends-chain.

    Resolution order: universal → base → instance (highest layer wins by id).
    Rules:
    - Instance may ADD (new id), OVERRIDE (same id), or list ids in `disable:` for
      base-DEFAULT rules (locked=False).
    - LOCKED rules (locked=True in a base layer) are IMMUTABLE: an instance override
      or disable targeting a locked id is SILENTLY IGNORED + WARNING logged.
    - There is no ALLOW severity in the engine (only BLOCK/WARN/AUDIT); an instance
      cannot add a permissive rule that defeats a BLOCK. This closes the "new-id ALLOW"
      weakening vector by construction — Severity("allow") raises ValueError on parse.

    Returns the merged list of Invariants.
    """
    # merged: id → Invariant (highest layer wins)
    merged: dict[str, Invariant] = {}

    for layer_name, doc in chain:
        layer_invariants = [
            _parse_invariant(raw, source_layer=layer_name)
            for raw in doc.get("invariants", [])
        ]
        disabled_ids: set[str] = set(doc.get("disable", []) or [])

        # Handle `disable:` list from this layer
        for inv_id in disabled_ids:
            if inv_id in merged and merged[inv_id].locked:
                _log.warning(
                    "Themis inheritance: layer %r attempted to disable locked rule %r "
                    "(defined in %r) — IGNORED. Locked rules cannot be disabled by instances.",
                    layer_name,
                    inv_id,
                    merged[inv_id].source_layer,
                )
            elif inv_id in merged:
                del merged[inv_id]

        # Handle invariant overrides/additions from this layer
        for inv in layer_invariants:
            if inv.id in merged and merged[inv.id].locked:
                _log.warning(
                    "Themis inheritance: layer %r attempted to override locked rule %r "
                    "(defined in %r) — IGNORED. Locked rules cannot be overridden by instances.",
                    layer_name,
                    inv.id,
                    merged[inv.id].source_layer,
                )
                # Keep the locked base rule; discard the instance's version.
                continue
            merged[inv.id] = inv

    return list(merged.values())


def load_rules(role: str) -> RuleSet:
    """Load and merge the rule set for `role` via the extends-chain.

    Resolution:
      1. Load <role>.yaml; if it has `extends: <base>`, follow the chain.
      2. Merge universal.base → <role>.base → instance by invariant id.
      3. LOCKED rules in base layers are immutable to instances.
      4. VALID_ROLES excludes *.base.yaml (bases are not assignable).

    No caching — re-reads and re-merges from disk on every call. A base-file
    change is live immediately on the next call.

    Raises:
        FileNotFoundError: No rule file for this role.
        ValueError: Cycle in extends-chain, or YAML parse failure.
    """
    # Fail-closed: if role resolves to a .base name it is NOT an assignable role.
    if role.endswith(".base"):
        raise FileNotFoundError(
            f"Role {role!r} is a base file, not an assignable role. "
            f"Use a concrete role name (e.g. 'backend-lead', not 'backend-lead.base')."
        )

    chain = _resolve_extends_chain(role)
    invariants = _merge_chain(role, chain)
    return RuleSet(role=role, invariants=invariants)


def load_rules_flat(role: str) -> RuleSet:
    """Load a rule set WITHOUT extends-chain resolution (single-file, legacy behavior).

    Used for migration-equivalence tests: compares pre-migration flat load against
    post-migration merged load for the 15 roles that should be behaviorally identical.
    """
    path = _RULES_DIR / f"{role}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No rule file for role {role!r}: {path}")
    doc = _load_doc(role)
    invariants = [
        _parse_invariant(raw, source_layer=role) for raw in doc.get("invariants", [])
    ]
    return RuleSet(role=role, invariants=invariants)


def list_rules(role: str) -> list[dict[str, str]]:
    """Return the merged ruleset with source-layer annotations (D3, debuggability).

    Each entry: {id, name, severity, source_layer, locked}.
    """
    ruleset = load_rules(role)
    return [
        {
            "id": inv.id,
            "name": inv.name,
            "severity": inv.severity.value,
            "source_layer": inv.source_layer,
            "locked": str(inv.locked),
        }
        for inv in ruleset.invariants
    ]


def load_all_rules() -> dict[str, RuleSet]:
    """Load every assignable role's merged rule set. Returns {role: RuleSet}."""
    return {role: load_rules(role) for role in VALID_ROLES}
