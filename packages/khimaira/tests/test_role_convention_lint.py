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


def test_master_md_contains_pre_askuserquestion_routing_table():
    """Regression guard: master.md must enumerate WHEN each routing target applies."""
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "pre-askuserquestion routing" in lowered, (
        "master.md missing 'pre-askuserquestion routing' section"
    )
    assert "architect-1" in lowered, "decision table missing architect-1 route"
    assert "critic-1" in lowered, "decision table missing critic-1 route"
    assert "analyst-1" in lowered, "decision table missing analyst-1 route"
    assert "verifier-1" in lowered, "decision table missing verifier-1 route"
    assert "personal preference" in lowered, "table missing legitimate user-preference case"
    assert "authorization" in lowered, "table missing irreversible-action case"


def test_question_text_is_design_shaped_fires_on_design_question():
    """New Themis condition — positive case: design-shape question fires."""
    from themis.conditions import question_text_is_design_shaped

    payload = {
        "tool_input": {
            "questions": [{"question": "Which approach should we take, A or B?"}]
        }
    }
    assert question_text_is_design_shaped(payload) is True


def test_question_text_is_design_shaped_skips_user_preference():
    """New Themis condition — negative case: user-preference must NOT fire."""
    from themis.conditions import question_text_is_design_shaped

    payload_feature = {
        "tool_input": {
            "questions": [{"question": "Which feature should ship in v2: dark mode or shortcuts?"}]
        }
    }
    assert question_text_is_design_shaped(payload_feature) is False

    payload_auth = {
        "tool_input": {
            "questions": [{"question": "Do you want me to delete the legacy migration?"}]
        }
    }
    assert question_text_is_design_shaped(payload_auth) is False


def test_intake_md_contains_roster_fan_out_checkpoint():
    """Regression guard: intake.md must articulate the roster-fan-out checkpoint
    + explicit-fan-out signals + ROSTER MAPPING template.

    Per bug-class enumeration msg-3ef13a423ecc. Joseph's load-bearing signal is
    'use all agents' — that phrase MUST appear in the signals section.
    """
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    # Checkpoint section header (case-insensitive — "roster-fan-out" or "roster fan-out")
    assert "roster-fan-out checkpoint" in lowered or "roster fan-out checkpoint" in lowered, (
        "intake.md missing 'roster-fan-out checkpoint' section"
    )
    # Joseph's load-bearing signal phrase
    assert "use all agents" in lowered, "intake.md missing 'use all agents' signal trigger"
    # ROSTER MAPPING template marker (case-sensitive for canonical use)
    assert "ROSTER MAPPING" in content, "intake.md missing 'ROSTER MAPPING' template marker"
    # Signal-conditional response (must NOT default to always-enumerate)
    assert "signal-conditional" in lowered or "signal conditional" in lowered, (
        "intake.md missing 'signal-conditional' framing — risks over-enumeration"
    )
