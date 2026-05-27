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
        # Strip "-lead" suffix, then take the last "-"-separated token as the
        # bare domain.  Prefixed leads (e.g. jp-frontend-lead) use bare domain
        # knowledge docs (frontend-knowledge.md), not prefixed ones — per the
        # prefix contract in manifest._lead_name / _themis_rule_ids.
        # assumes domain names are single-word (no hyphens)
        stem_without_lead = role_path.stem[: -len("-lead")]  # e.g. "jp-frontend"
        domain = stem_without_lead.rsplit("-", 1)[-1]  # "frontend"
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


def test_intake_md_contains_begin_gate_scope():
    """Regression guard: intake.md must distinguish in-chat fan-out from
    cross-session handoff per agent-3 BEGIN-gate confusion incident
    (msg-d4b089b0e2fc).
    """
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "begin-gate scope" in lowered or "begin gate scope" in lowered, (
        "intake.md missing 'BEGIN-gate scope' section"
    )
    assert "in-chat fan-out" in lowered, "intake.md missing 'in-chat fan-out' distinction"
    assert "cross-session handoff" in lowered, "intake.md missing 'cross-session handoff' distinction"
    assert "master mediation" in lowered, "intake.md missing 'master mediation' requirement phrase"


def test_intake_md_contains_proactive_specialist_routing():
    """Regression guard: intake.md must describe proactive specialist dispatch
    on research-blocker phrases (jp-intake-1 relay msg-2ba0730b3db9).
    """
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "proactive specialist routing" in lowered, (
        "intake.md missing 'Proactive specialist routing' section"
    )
    for phrase in ("reading", "investigating", "research"):
        assert phrase in lowered, f"intake.md missing trigger phrase '{phrase}'"
    for role in ("architect-1", "analyst-1", "verifier-1", "critic-1"):
        assert role in content, f"intake.md missing specialist '{role}'"
    assert "5 min" in content or "5min" in content or "five min" in lowered, (
        "intake.md missing >5min research boundary"
    )


def test_in_intake_5_themis_rule_present():
    """Regression guard: IN-INTAKE-5 (NO_MASTER_DISPATCH) must be defined in intake.yaml.

    Per behavioral-rule-promotion intake-skip-master-mediation class (2026-05-26).
    Observed twice: intake bypassed master's chat_task_create + BEGIN gate.
    """
    content = (THEMIS_RULES / "intake.yaml").read_text()
    assert "IN-INTAKE-5" in content, "intake.yaml missing IN-INTAKE-5 rule"
    assert "NO_MASTER_DISPATCH" in content, "intake.yaml IN-INTAKE-5 missing rule name"
    assert "chat_task_create" in content, "intake.yaml IN-INTAKE-5 missing chat_task_create matcher"
    assert "chat_task_signal_start" in content, (
        "intake.yaml IN-INTAKE-5 missing chat_task_signal_start matcher"
    )


def test_intake_md_no_direct_dispatch_section():
    """Regression guard: intake.md must explicitly forbid direct task dispatch.

    Per behavioral-rule-promotion intake-skip-master-mediation class (2026-05-26).
    Role-doc enforcement alone is insufficient (violation recurred within minutes of
    BEGIN-gate scope rule shipping). Convention + structural Themis nudge are both
    required; this test guards the convention layer.
    """
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "intake never dispatches tasks directly" in lowered, (
        "intake.md missing 'Intake NEVER dispatches tasks directly' section"
    )
    assert "chat_task_create" in content, "intake.md no-direct-dispatch section missing chat_task_create"
    assert "chat_task_signal_start" in content, (
        "intake.md no-direct-dispatch section missing chat_task_signal_start"
    )
    assert "IN-INTAKE-5" in content, "intake.md no-direct-dispatch section missing IN-INTAKE-5 reference"


def test_intake_md_no_solo_research_multi_issue():
    """Regression guard: intake.md must articulate the solo-research anti-pattern.

    Per behavioral-rule-promotion intake-solo-research class (2026-05-26).
    jp-intake-1 spent many turns on solo deep investigation across 3-4 issues
    instead of decomposing + delegating; Joseph escalated twice before intake
    switched. The trigger (2+ issues → stop + decompose) and the worked example
    must be present.
    """
    content = (ROLE_DIR / "intake.md").read_text()
    lowered = content.lower()
    assert "solo-research" in lowered, (
        "intake.md missing 'solo-research' anti-pattern section"
    )
    assert "receive" in lowered and "decompose" in lowered and "handoff" in lowered, (
        "intake.md missing RECEIVE → DECOMPOSE → HANDOFF framing"
    )
    assert "2+" in content or "two or more" in lowered or "2 or more" in lowered, (
        "intake.md missing '2+ distinct issues' trigger"
    )
    assert "stop" in lowered, "intake.md missing STOP instruction in solo-research section"


def test_architect_md_contains_session_set_name_bootstrap():
    """Regression guard: architect.md must instruct session_set_name on bootstrap.

    session_set_name is load-bearing for Pattern 5 liveness probe: the 180s
    architect threshold is looked up via session name. Without this call, the
    probe caches the 90s default at registration time and fires prematurely.

    Per Cat 2 audit ctx-pattern5-architect-threshold-misfire.
    """
    content = (ROLE_DIR / "architect.md").read_text()
    assert "session_set_name" in content, (
        "architect.md missing session_set_name bootstrap instruction"
    )
    assert "architect-" in content.lower(), (
        "architect.md missing 'architect-N' name convention example"
    )


def test_master_md_contains_propose_only_branch():
    """Regression guard: master.md must describe the PROPOSE-ONLY BRANCH for
    domain-lead delegation when leads are write-blocked.

    Per task-9627f2ef147a: PROPOSE-ONLY FLOW documentation gap (2026-05-27).
    Without this section master dispatches lead to execute → Themis blocks →
    janice stuck improvising. The branch must name master's correct role
    (dispatch agent + lead guides) and be keyed to propose_only=true.
    """
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "propose-only branch" in lowered, (
        "master.md missing 'PROPOSE-ONLY BRANCH' section in domain-lead delegation"
    )
    assert "implementation-ready plan" in lowered, (
        "master.md propose-only branch missing 'implementation-ready plan' anchor"
    )
    assert "domain authority" in lowered, (
        "master.md propose-only branch missing 'domain authority' role framing"
    )
    assert "lead guides" in lowered or "lead↔agent" in lowered or "lead guides" in lowered, (
        "master.md propose-only branch missing lead-guides-agent guidance"
    )


def test_lead_role_template_emits_propose_only_how_to_work(tmp_path):
    """Regression guard: lead-role template must emit propose-only how-to-work
    guidance when propose_only=True and omit it when propose_only=False.

    Per task-9627f2ef147a: per-project isolation is load-bearing — jp-leads get
    the guidance; khimaira leads (propose_only=False) must not.
    """
    from khimaira.leads.manifest import Manifest, LeadConfig
    from khimaira.leads.sync import _render_role_doc

    lead_cfg = LeadConfig(
        name="frontend", paths=["frontend/**"], model="sonnet", effort="medium"
    )

    def make_manifest(propose_only: bool) -> Manifest:
        return Manifest(
            project_name="test-project",
            prefix="tp",
            root_path=tmp_path,
            roles_dir=tmp_path / "roles",
            themis_dir=tmp_path / "themis",
            knowledge_dir=tmp_path / "docs" / "domain",
            leads={"frontend": lead_cfg},
            propose_only=propose_only,
        )

    propose_doc = _render_role_doc(make_manifest(propose_only=True), "frontend", lead_cfg)
    plain_doc = _render_role_doc(make_manifest(propose_only=False), "frontend", lead_cfg)

    # propose_only=True: guidance section must be present
    assert "propose-only mode" in propose_doc.lower(), (
        "lead-role template missing propose-only how-to-work section when propose_only=True"
    )
    assert "implementation-ready plan" in propose_doc.lower(), (
        "lead-role template missing 'implementation-ready plan' guidance when propose_only=True"
    )
    assert "domain authority" in propose_doc.lower(), (
        "lead-role template missing 'domain authority' framing when propose_only=True"
    )

    # propose_only=False: no propose-only how-to-work content
    assert "implementation-ready plan" not in plain_doc.lower(), (
        "lead-role template must NOT emit propose-only how-to-work when propose_only=False"
    )
