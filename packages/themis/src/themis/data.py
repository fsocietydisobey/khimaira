"""Data model for Themis rule definitions and evaluation results.

Core types:
  Matcher        — one tool-match criterion (exact name or tool_input_field regex)
  Invariant      — a rule: id, severity, matchers (OR-combined), optional conditions
  RuleSet        — parsed collection of invariants for one role
  EvalResult     — outcome of evaluate(): ok=True (pass) or ok=False + violation detail
  ViolationRecord — a logged violation entry (written to JSONL by violations.py)

YAML is loaded via load_rules(role) which finds the bundled rule file by role name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

# Bundled rules directory, co-located with this package.
_RULES_DIR = Path(__file__).parent / "rules"

VALID_ROLES = frozenset(
    ["intake", "master", "agent", "observer", "architect", "analyst", "verifier", "critic"]
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

    def tool_matches(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """True if at least one matcher matches (OR-combined)."""
        return any(m.matches(tool_name, tool_input) for m in self.matchers)


@dataclass
class RuleSet:
    """All invariants for one role, loaded from a YAML file."""

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


def _parse_invariant(raw: dict[str, Any]) -> Invariant:
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
    )


def load_rules(role: str) -> RuleSet:
    """Load and parse the rule YAML for `role`.

    Raises:
        ValueError: Unknown role or invalid YAML schema.
        FileNotFoundError: Rule file missing from the package.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role {role!r}. Valid roles: {sorted(VALID_ROLES)}")
    path = _RULES_DIR / f"{role}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Rule file not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"Rule file {path} must be a YAML mapping at the top level")
    invariants = [_parse_invariant(inv) for inv in doc.get("invariants", [])]
    return RuleSet(role=role, invariants=invariants)


def load_all_rules() -> dict[str, RuleSet]:
    """Load every role's rule set. Returns {role: RuleSet}."""
    return {role: load_rules(role) for role in VALID_ROLES}
