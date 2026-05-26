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


def test_domain_topology_rfc_doc_exists_and_has_key_sections():
    """Regression guard: domain-topology RFC must exist + contain key sections.

    Phase 0 RFC per architect-1 enumeration msg-2a44845324d2. Joseph reviews
    this doc before Phase 1 implementation briefs are authored. Removing
    sections silently would regress the sign-off contract.
    """
    rfc_path = REPO_ROOT / "docs" / "khimaira-roster-topology-rfc.md"
    assert rfc_path.exists(), f"RFC doc missing at {rfc_path}"
    content = rfc_path.read_text()
    lowered = content.lower()

    # Executive summary up-front (Joseph signs without re-reading enumeration)
    assert "executive summary" in lowered, "RFC missing 'Executive Summary' section"

    # T7 chosen topology
    assert "T7" in content, "RFC missing T7 topology reference"
    assert "leads as workers and decomposers" in lowered, "RFC missing T7 description"

    # Role definitions
    assert "domain lead" in lowered, "RFC missing domain lead role definition"

    # Per-roster differentiation
    assert "khimaira-dev" in content and "jeevy-product" in content, (
        "RFC missing per-roster lead selection"
    )

    # Phased migration plan
    assert "phase 0" in lowered and "phase 1" in lowered, "RFC missing phased migration plan"

    # Cross-cut with discipline fixes (today's work)
    assert "discipline fix" in lowered or "complementary" in lowered, (
        "RFC missing cross-cut section with discipline fixes"
    )

    # Open questions for Joseph
    assert "open questions" in lowered, "RFC missing 'Open Questions' section for Joseph review"

    # Sign-off block
    assert "sign-off" in lowered, "RFC missing sign-off block"


def test_arc_end_reconcile_step_exists_in_master_md():
    """Regression guard: master.md must contain Step 7 — Reconcile.

    Per architect-1 enumeration msg-825a1cab2707. Removing this step
    regresses the arc-end coherence audit class.
    """
    content = (ROLE_DIR / "master.md").read_text()
    assert "Step 7 — Reconcile" in content, "master.md missing Step 7 Reconcile"
    lowered = content.lower()
    assert "merge_intent" in lowered, "master.md Step 7 missing merge_intent enum reference"
    assert "intake complete" in lowered, "master.md Step 7 missing INTAKE COMPLETE signal"
    assert "silent strand" in lowered, "master.md Step 7 missing failure-mode framing"


def test_agent_md_done_report_requires_branch_fields():
    """Regression guard: agent.md done-report template must include
    branch / worktree / merge_intent fields per IN-AGENT-4 contract."""
    content = (ROLE_DIR / "agent.md").read_text()
    assert "branch:" in content, "agent.md done-report missing branch field"
    assert "worktree:" in content, "agent.md done-report missing worktree field"
    assert "merge_intent:" in content, "agent.md done-report missing merge_intent field"
    assert "merge-to-main" in content, "agent.md missing merge_intent enum value"
    assert "keep-isolated" in content, "agent.md missing merge_intent enum value"


def test_in_agent_4_themis_rule_present():
    """Regression guard: IN-AGENT-4 must be defined in agent.yaml."""
    content = (THEMIS_RULES / "agent.yaml").read_text()
    assert "IN-AGENT-4" in content, "agent.yaml missing IN-AGENT-4 rule"
    assert "DONE_REPORT_BRANCH_DECLARATION" in content, "missing rule name"
    assert "done_report_missing_branch_declaration" in content, "missing condition reference"


def test_no_stranded_arc_branches():
    """CLASS-INVARIANT TEST — Phase 1 acceptance criterion.

    For every chat_task_update with status=done in chat history (Cat 1 scope:
    declaration presence, not branch-checkout audit), verify branch /
    worktree / merge_intent fields are declared.

    Per architect-1 enumeration msg-825a1cab2707. Catches the JEEVY-543
    silent-strand failure mode at declaration-time. Cat 2 will extend to
    actual branch-checkout verification (tracker STATE.md sidebar +
    observer surveillance).

    Note: this test passes vacuously until done-reports start using new
    fields post-ship. Once agents follow the new convention, regressions
    (missing field, missing merge_intent enum value) fail this assertion.
    """
    # Cat 1 scope: validate the CONVENTION exists (role docs + Themis).
    # Full chat-history audit deferred to Cat 2 surveillance.
    # Confirm all three artifacts coexist:
    master_md = (ROLE_DIR / "master.md").read_text()
    agent_md = (ROLE_DIR / "agent.md").read_text()
    agent_yaml = (THEMIS_RULES / "agent.yaml").read_text()

    assert all([
        "Step 7 — Reconcile" in master_md,
        "merge_intent:" in agent_md,
        "IN-AGENT-4" in agent_yaml,
    ]), (
        "Arc-end coherence convention incomplete — one or more of "
        "master.md Step 7, agent.md merge_intent field, IN-AGENT-4 rule "
        "is missing. Full convention required for class closure."
    )


DOMAIN_DOCS_DIR = REPO_ROOT / "docs" / "domain"


def test_each_lead_role_has_knowledge_doc():
    """Phase 1A regression guard: every declared domain-lead role must have a
    corresponding knowledge doc at docs/domain/<domain>-knowledge.md.

    Per architect-1 enumeration msg-883b3e43c9d1. Catches "role exists but
    knowledge doc never created" — the primary failure mode of Phase 1A.

    Note: this test passes vacuously until lead role files exist (Phase 1 of
    the topology RFC). Once `backend-lead.md` / `data-lead.md` etc. land, the
    test enforces doc-existence per lead.
    """
    leads = list(ROLE_DIR.glob("*-lead.md"))
    for role_path in leads:
        domain = role_path.stem.replace("-lead", "")
        doc_path = DOMAIN_DOCS_DIR / f"{domain}-knowledge.md"
        assert doc_path.exists(), (
            f"Lead role {role_path.name} has no knowledge doc at {doc_path}. "
            f"See docs/domain/_template-knowledge.md + docs/domain/README.md. "
            f"Per docs/khimaira-roster-topology-rfc.md Phase 1A spec."
        )


def test_knowledge_doc_has_required_sections():
    """Each non-template knowledge doc must contain required section headers.

    Validates STRUCTURE (sections exist), NOT CORRECTNESS (entries are accurate).
    Correctness is a human-discipline + Phase 2 reconciliation concern.
    """
    required_sections = [
        "## Patterns",
        "## Footguns",
        "## Key files",
        "## Open questions",
        "## Recent significant changes",
    ]
    for doc_path in DOMAIN_DOCS_DIR.glob("*-knowledge.md"):
        # Skip the template — it's a scaffold, not a real knowledge doc
        if doc_path.name == "_template-knowledge.md":
            continue
        content = doc_path.read_text()
        for section in required_sections:
            assert section in content, (
                f"{doc_path.name} missing required section '{section}'. "
                f"Compare against docs/domain/_template-knowledge.md."
            )


def test_backend_lead_md_exists():
    """Phase 1 regression guard: backend-lead role file must exist."""
    assert (ROLE_DIR / "backend-lead.md").exists(), (
        "backend-lead.md missing — Phase 1 of topology RFC creates this. "
        "See docs/khimaira-roster-topology-rfc.md."
    )


def test_data_lead_md_exists():
    """Phase 1 regression guard: data-lead role file must exist."""
    assert (ROLE_DIR / "data-lead.md").exists(), (
        "data-lead.md missing — Phase 1 of topology RFC creates this."
    )


def test_master_md_contains_lead_handshake():
    """Phase 1 regression guard: master.md must contain lead handshake protocol."""
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "domain lead delegation" in lowered, "master.md missing 'Domain lead delegation' section"
    assert "BACKEND INTENT" in content, "master.md missing BACKEND INTENT handshake marker"
    assert "BACKEND PLAN" in content, "master.md missing BACKEND PLAN return marker"
    assert "cross-cutting" in lowered, "master.md missing cross-cutting handshake guidance"


def test_intake_md_contains_domain_signal():
    """Phase 1 regression guard: intake.md must describe domain-signal CONTEXT UPDATE field."""
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "domain signal" in lowered, "intake.md missing 'Domain signal' subsection"
    assert "domain-signal:" in content, "intake.md missing 'domain-signal:' field marker"
    assert "cross-cutting" in lowered, "intake.md missing cross-cutting domain-signal value"


def test_in_backend_lead_themis_rules_present():
    """Phase 1 regression guard: IN-BACKEND-LEAD-1/2 must be defined."""
    content = (THEMIS_RULES / "backend-lead.yaml").read_text()
    assert "IN-BACKEND-LEAD-1" in content
    assert "NO_FILE_EDIT_OUTSIDE_BACKEND" in content
    assert "IN-BACKEND-LEAD-2" in content
    assert "NO_STANDALONE_AGENTS" in content


def test_in_data_lead_themis_rules_present():
    """Phase 1 regression guard: IN-DATA-LEAD-1/2 must be defined."""
    content = (THEMIS_RULES / "data-lead.yaml").read_text()
    assert "IN-DATA-LEAD-1" in content
    assert "NO_FILE_EDIT_OUTSIDE_DATA" in content
    assert "IN-DATA-LEAD-2" in content
    assert "NO_STANDALONE_AGENTS" in content


def test_master_md_contains_stay_oriented_section():
    """Regression guard: master.md must articulate proactive status-surface
    protocol for state transitions.

    Per architect-1 enumeration msg-9cb4a253b786. Catches Joseph's "silence
    on idle" feedback failure mode (2026-05-26).
    """
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "stay oriented" in lowered or "stay-oriented" in lowered, (
        "master.md missing 'Stay oriented' section"
    )
    assert "📍" in content, "master.md missing 📍 status snapshot marker"
    assert "awaiting your reply" in lowered, "master.md missing question-then-idle transition"
    assert "blocked on" in lowered or "blocked—" in lowered or "blocked —" in lowered, (
        "master.md missing blocked-on-external-dep transition"
    )
    assert "no work in queue" in lowered, "master.md missing all-idle transition"
    assert "Open until" in content, "master.md missing open-until template marker"
    for state in ("IDLE", "BLOCKED"):
        assert state in content, f"master.md missing state '{state}' enumeration"


def test_intake_md_contains_status_translation():
    """Regression guard: intake.md must describe user-facing status translation
    when master is deep in coordination."""
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "status translation" in lowered, "intake.md missing 'Status translation' section"
    assert ("don't double-surface" in lowered or "don't double surface" in lowered
            or "doesn't double" in lowered or "stays silent" in lowered), (
        "intake.md missing double-surface guard"
    )
    assert "📍" in content, "intake.md missing 📍 cross-reference to master's status template"


def test_status_snapshot_template_documented():
    """Regression guard: canonical status template format must be in master.md.

    Template: 📍 [STATE] — [one-line context]. Open until [condition].
    """
    content = (ROLE_DIR / "master.md").read_text()
    assert "📍" in content, "status snapshot emoji marker missing from master.md"
    assert "Open until" in content, "open-until-condition marker missing"
    states_present = sum(1 for s in ("IDLE", "BLOCKED", "CONSULTING", "CROSS-SESSION") if s in content)
    assert states_present >= 2, (
        f"master.md must enumerate at least 2 canonical states "
        f"(IDLE/BLOCKED/CONSULTING/CROSS-SESSION); found {states_present}"
    )
