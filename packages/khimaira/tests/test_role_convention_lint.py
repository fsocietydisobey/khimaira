"""Lint tests guarding role-doc + Themis-rule conventions from silent regression.

These are dumb-but-deterministic checks: if someone removes the convention text
from master.md / architect.md / master.yaml, the test fails. Same pattern as
the existing Themis rule-presence tests in test_role_coverage.py.

Per bug-class enumeration msg-2253225590cf. The class IS behavioral; unit tests
can't assert "master parallelizes correctly." They CAN assert "the convention
that nudges master to parallelize is still on disk."
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE_DIR = REPO_ROOT / "packages/khimaira/src/khimaira/roles"
THEMIS_RULES = REPO_ROOT / "packages/themis/src/themis/rules"


def test_master_md_contains_pre_dispatch_checkpoint():
    """Regression guard: removing the checkpoint phrase regresses the class."""
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "pre-dispatch" in lowered, "master.md missing 'pre-dispatch' phrase"
    assert "independence checkpoint" in lowered, "master.md missing 'independence checkpoint'"
    assert "PARALLEL-CAPABLE" in content, "master.md missing PARALLEL-CAPABLE cross-reference"


def test_architect_md_contains_parallel_capable_convention():
    """Regression guard: architect-reply convention for parallel surfacing."""
    content = (ROLE_DIR / "architect.md").read_text()
    assert "PARALLEL-CAPABLE while you wait" in content, "architect.md missing convention header"
    assert "consult-reply convention" in content.lower(), "architect.md missing convention framing"
    assert "When to OMIT" in content or "when to omit" in content.lower()


def test_in_master_6_themis_rule_present():
    """Regression guard: IN-MASTER-6 must be defined in master.yaml."""
    content = (THEMIS_RULES / "master.yaml").read_text()
    assert "IN-MASTER-6" in content, "master.yaml missing IN-MASTER-6 rule"
    assert "CONSIDER_BATCHING_RECENT_DISPATCH" in content, "missing rule name"
    assert "recent_dispatch_different_ctx" in content, "missing condition check reference"
