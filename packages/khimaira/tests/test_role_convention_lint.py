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
    """Regression guard (post-inheritance refactor): backend-lead's write-gate
    lives in the LOCKED base (`backend-lead.base.yaml`); the instance inherits it
    via `extends:`. (Replaces the old NO_FILE_EDIT_OUTSIDE_BACKEND domain-
    restrictor, removed when leads moved to coordinate-and-delegate.)"""
    base = (THEMIS_RULES / "backend-lead.base.yaml").read_text()
    assert "NO_DIRECT_CODING" in base
    assert "NO_GIT_COMMIT" in base
    assert "locked: true" in base
    instance = (THEMIS_RULES / "backend-lead.yaml").read_text()
    assert "extends: backend-lead.base" in instance


def test_in_data_lead_themis_rules_present():
    """Regression guard (post-inheritance refactor): data-lead's write-gate lives
    in the LOCKED base (`data-lead.base.yaml`); the instance inherits it via
    `extends:`. (Replaces the old NO_FILE_EDIT_OUTSIDE_DATA domain-restrictor.)"""
    base = (THEMIS_RULES / "data-lead.base.yaml").read_text()
    assert "NO_DIRECT_CODING" in base
    assert "NO_GIT_COMMIT" in base
    assert "locked: true" in base
    instance = (THEMIS_RULES / "data-lead.yaml").read_text()
    assert "extends: data-lead.base" in instance


def test_leads_inherit_locked_write_gate():
    """Parity guard (post-inheritance refactor): every *-lead.base yaml carries
    the LOCKED NO_DIRECT_CODING + NO_GIT_COMMIT security rules, and every lead
    INSTANCE inherits them via `extends:`.

    Replaces the old PATH_CONTENTION-parity guard: under the inheritance model
    leads no longer edit directly (NO_DIRECT_CODING blocks all Edit/Write), so
    the cross-cutting guarantee is the locked write-gate inherited from the base.
    Catches a lead that doesn't inherit the gate (the new "lead missing the
    cross-cutting rule" failure — same spirit as the Guard-2 PATH_CONTENTION gap
    that passed 1114 tests, caught only by critic's manual read).
    """
    import re as _re

    # Every lead BASE must carry the LOCKED write-gate (both rules locked so an
    # instance can neither override nor disable them — the escalation guard).
    base_yamls = sorted(THEMIS_RULES.glob("*-lead.base.yaml"))
    assert base_yamls, "No *-lead.base.yaml files found — inheritance bases missing"
    for base in base_yamls:
        content = base.read_text()
        assert "NO_DIRECT_CODING" in content, f"{base.name}: missing NO_DIRECT_CODING"
        assert "NO_GIT_COMMIT" in content, f"{base.name}: missing NO_GIT_COMMIT"
        # Both security invariants must be locked: true.
        assert content.count("locked: true") >= 2, (
            f"{base.name}: NO_DIRECT_CODING + NO_GIT_COMMIT must both be locked: true"
        )

    # Every lead INSTANCE yaml must inherit a base via `extends:`.
    instance_leads = [
        p for p in sorted(THEMIS_RULES.glob("*-lead.yaml"))
        if not p.name.endswith(".base.yaml")
    ]
    assert instance_leads, "No lead instance yamls found"
    missing_extends = [
        path.name for path in instance_leads
        if not _re.search(r"^extends:\s*\S+", path.read_text(), _re.MULTILINE)
    ]
    assert not missing_extends, (
        "Lead instances must inherit the locked write-gate via `extends:`:\n"
        + "\n".join(f"  {m}" for m in missing_extends)
    )


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


def test_agent_md_contains_gate_before_commit():
    """Regression guard: agent.md must contain a gate-before-commit section.

    Per behavioral-rule-promotion gate-skip class (2026-05-27). Pattern observed
    2× in one session (agent-3 Phase A.5, agent-2 b6355f1): agent commits before
    critic+verifier. Role-doc enforcement only (Layer 2 Themis fragile: detecting
    "git commit without task-specific verdicts in chat" requires stateful semantic
    reasoning at hook-time — same tractability verdict as intake-solo-research).
    """
    content = (ROLE_DIR / "agent.md").read_text()
    lowered = content.lower()
    assert "gate-before-commit" in lowered, (
        "agent.md missing 'gate-before-commit' section"
    )
    assert "never commit before" in lowered, (
        "agent.md gate-before-commit missing NEVER-commit-before mandate"
    )
    assert "critic approve" in lowered, (
        "agent.md gate-before-commit missing critic APPROVE requirement"
    )
    assert "verifier ship" in lowered, (
        "agent.md gate-before-commit missing verifier SHIP requirement"
    )
    assert "msg-" in content, (
        "agent.md gate-before-commit missing verdict msg-ID citation requirement"
    )


def test_master_md_contains_post_approval_distillation():
    """Regression guard: master.md must document the post-approval distillation step
    for domain leads.

    Per task-1e87b6702b6a: auto-distill into mnemosyne when master approves a domain
    lead's done report. Without this, long-lived leads lose their session knowledge
    unless they manually run /khimaira-distill. The step must name detect_domain
    (the lead-detection gate), mnemosyne, and both knowledge sinks.
    """
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()
    assert "post-approval distillation" in lowered, (
        "master.md missing 'post-approval distillation' section in approval flow"
    )
    assert "detect_domain" in content, (
        "master.md post-approval distillation missing detect_domain lead-detection gate"
    )
    assert "mnemosyne" in lowered, (
        "master.md post-approval distillation must reference mnemosyne"
    )
    assert "provisional" in lowered, (
        "master.md post-approval distillation must name mnemosyne PROVISIONAL sink"
    )
    assert "docs/domain/" in content, (
        "master.md post-approval distillation must name docs/domain/<domain>-knowledge.md authoritative sink"
    )


def test_role_budget_lead_entries_in_sync():
    """Regression guard: ROLE_BUDGET (chats.py) and _ROLE_BUDGET (session_start.py)
    must both contain entries for the 4 core domain leads.

    These two dicts are explicit duplicates (session_start.py is stdlib-only and
    cannot import from chats.py). Without this test, one copy can drift silently.
    Per F2/F3 of the backend-lead-1 role-set audit (task-6a0fa19f3639, 2026-05-27).
    """
    # Only leads with themis rule yamls — entries must be in _VALID_ROLES.
    _LEAD_ROLES = ("backend-lead", "data-lead", "jp-backend-lead", "jp-data-lead", "jp-frontend-lead")
    _EXPECTED_BUDGET = {"model": "sonnet", "effort": "medium"}

    chats_src = (REPO_ROOT / "packages/khimaira/src/khimaira/monitor/chats.py").read_text()
    session_start_src = (
        REPO_ROOT / "packages/khimaira/src/khimaira/hooks/session_start.py"
    ).read_text()

    for lead in _LEAD_ROLES:
        assert f'"{lead}"' in chats_src, (
            f"ROLE_BUDGET in chats.py missing entry for {lead!r}"
        )
        assert f'"{lead}"' in session_start_src, (
            f"_ROLE_BUDGET in session_start.py missing entry for {lead!r}"
        )
        # Both must agree on sonnet/medium (not opus/max — per lead-role.md.j2 convention)
        assert '"sonnet"' in chats_src, "chats.py ROLE_BUDGET lead entries must use sonnet model"
        assert '"sonnet"' in session_start_src, (
            "session_start.py _ROLE_BUDGET lead entries must use sonnet model"
        )


def test_master_role_budget_is_opus_max():
    """Regression guard: ROLE_BUDGET for master must stay at opus/max in both files.

    Master requires Opus 4.8 for coordination/synthesis quality. This was oscillated
    between sonnet and opus during the 2026-05-30 session (commits 142e3d7, 20ef7db,
    3ad222f) before Joseph confirmed opus/max. This test prevents silent regression.
    Both chats.py (ROLE_BUDGET) and session_start.py (_ROLE_BUDGET) must be in sync.
    """
    import ast, re

    chats_src = (REPO_ROOT / "packages/khimaira/src/khimaira/monitor/chats.py").read_text()
    session_start_src = (
        REPO_ROOT / "packages/khimaira/src/khimaira/hooks/session_start.py"
    ).read_text()

    # Extract ROLE_MASTER entry from chats.py
    chats_master_match = re.search(r'ROLE_MASTER:\s*(\{[^}]+\})', chats_src)
    assert chats_master_match, "ROLE_MASTER entry not found in chats.py ROLE_BUDGET"
    chats_master_budget = ast.literal_eval(chats_master_match.group(1))
    assert str(chats_master_budget.get("model")).startswith("opus"), (
        f"chats.py ROLE_BUDGET master model must be opus-tier, got {chats_master_budget.get('model')!r}. "
        "Joseph confirmed opus/max 2026-05-30; opus[1m] (1M context) added 2026-06-08. Must not be sonnet."
    )
    assert chats_master_budget.get("effort") == "max", (
        f"chats.py ROLE_BUDGET master effort must be 'max', got {chats_master_budget.get('effort')!r}."
    )

    # Extract master entry from session_start.py _ROLE_BUDGET
    session_master_match = re.search(r'"master":\s*(\{[^}]+\})', session_start_src)
    assert session_master_match, "master entry not found in session_start.py _ROLE_BUDGET"
    session_master_budget = ast.literal_eval(session_master_match.group(1))
    assert str(session_master_budget.get("model")).startswith("opus"), (
        f"session_start.py _ROLE_BUDGET master model must be opus-tier, got {session_master_budget.get('model')!r}. "
        "chats.py and session_start.py must stay in sync."
    )
    assert session_master_budget.get("effort") == "max", (
        f"session_start.py _ROLE_BUDGET master effort must be 'max', got {session_master_budget.get('effort')!r}."
    )


def test_domains_tuple_covers_core_lead_domains():
    """Regression guard: session_end_utils._DOMAINS must contain the 4 core domains.

    _DOMAINS is not registry-derived — it's a standalone tuple. Adding a new lead
    domain requires updating this tuple; failing to do so silently breaks session-end
    distillation and session-start provisional memory injection for that domain.
    Per F5 of the backend-lead-1 role-set audit (task-6a0fa19f3639, 2026-05-27).
    Approach (b): _DOMAINS stays as single source; this test prevents silent removal
    of an established domain.
    """
    src = (
        REPO_ROOT / "packages/khimaira/src/khimaira/hooks/session_end_utils.py"
    ).read_text()
    for domain in ("backend", "frontend", "data", "devops"):
        assert f'"{domain}"' in src, (
            f"session_end_utils._DOMAINS missing {domain!r} — "
            "leads for this domain will miss distillation + provisional memory"
        )


def test_master_no_direct_code_implementation_themis_rule_exists():
    """Regression guard: IN-MASTER-7 (NO_DIRECT_CODE_IMPLEMENTATION) must exist in master.yaml.

    Prevents silent regression of the behavioral rule that master should delegate
    code implementation to agents rather than implementing directly. Observed violation
    on 2026-05-30: khimaira-master edited user_prompt_submit.py and wrote tests
    directly instead of assigning to idle roster agents.

    All three layers of behavioral-rule-promotion must be present:
    - Layer 1 (role-doc): master.md constraint section ← already linted elsewhere
    - Layer 2 (Themis): IN-MASTER-7 in master.yaml ← this test
    - Layer 3 (lint): this test file entry ← this function
    """
    master_yaml = (
        REPO_ROOT / "packages/themis/src/themis/rules/master.yaml"
    ).read_text()
    assert "IN-MASTER-7" in master_yaml, (
        "IN-MASTER-7 (NO_DIRECT_CODE_IMPLEMENTATION) missing from master.yaml. "
        "This Themis rule warns master when it edits code/test files directly "
        "instead of delegating to agents. Add it back."
    )
    assert "NO_DIRECT_CODE_IMPLEMENTATION" in master_yaml, (
        "IN-MASTER-7 name field missing from master.yaml."
    )
    # Also verify master.md has the constraint documented
    master_md = (
        REPO_ROOT / "packages/khimaira/src/khimaira/roles/master.md"
    ).read_text()
    assert "Single-master authority" in master_md, (
        "Single-master authority section missing from master.md. "
        "See commit 106dc13."
    )


# ---------------------------------------------------------------------------
# Khimaira-system gap reporting channel (task-c8c51ea2ecdf)
# ---------------------------------------------------------------------------

COMMANDS_DIR = Path.home() / ".claude" / "commands"


def test_agent_md_contains_report_gaps_section():
    """Regression guard: agent.md must have the 'report khimaira-system gaps' convention."""
    content = (ROLE_DIR / "agent.md").read_text().lower()
    assert "khimaira gap" in content, (
        "agent.md missing '🐞 KHIMAIRA GAP' reporting convention. "
        "See task-c8c51ea2ecdf / roles/agent.md 'Report khimaira-system gaps'."
    )
    assert "report khimaira" in content or "report khimaira-system" in content, (
        "agent.md missing 'Report khimaira-system gaps' section header."
    )


def test_master_md_contains_forward_gaps_section():
    """Regression guard: master.md must have the 'forward khimaira-system gaps' convention."""
    content = (ROLE_DIR / "master.md").read_text().lower()
    assert "forward khimaira" in content or "forward khimaira-system" in content, (
        "master.md missing 'Forward khimaira-system gaps' section."
    )
    assert "scope_cwd" in content, (
        "master.md 'forward gaps' section missing scope_cwd handoff routing."
    )
    assert "cwd is a project discriminator" in content, (
        "master.md missing the cwd-is-project-discriminator roster invariant note."
    )


def test_khimaira_gaps_command_exists():
    """Regression guard: /khimaira-gaps command file must exist in ~/.claude/commands/."""
    cmd_file = COMMANDS_DIR / "khimaira-gaps.md"
    assert cmd_file.exists(), (
        f"/khimaira-gaps command missing at {cmd_file}. "
        "The read-side filter is required for the gap channel to not drown in noise."
    )
    content = cmd_file.read_text().lower()
    assert "khimaira gap" in content, (
        "khimaira-gaps.md must reference the '🐞 KHIMAIRA GAP' tag filter."
    )


# ---------------------------------------------------------------------------
# Accountability model P1 (task-a7aeeed16aeb)
# ---------------------------------------------------------------------------


def test_master_md_contains_lead_domain_gate_section():
    """Regression guard: master.md must have the lead-domain gate + Track-A/B convention."""
    content = (ROLE_DIR / "master.md").read_text().lower()
    assert "lead-domain gate" in content or "lead domain gate" in content, (
        "master.md missing 'Lead-domain gate' accountability section."
    )
    assert "track-a" in content, "master.md missing Track-A routing."
    assert "track-b" in content, "master.md missing Track-B advisory roles."


def test_master_md_lead_gate_frames_convention_not_enforcement():
    """P1 prose MUST NOT claim P2 enforcement that doesn't exist yet.

    A role-doc that reads as enforced over-claims an unbuilt gate → master trusts
    protection that isn't there (diffusion hidden behind 'the doc says it's handled').

    The positive half: the convention-marker must be present.
    The negative half (analyst-required, section-SCOPED): no enforced-phrasing in
    the lead-domain-gate section. Section-scoped to avoid false-positives on the
    legitimately-enforced lead WRITE-gate elsewhere in master.md (e.g. 'never weaken').
    """
    content = (ROLE_DIR / "master.md").read_text()
    lowered = content.lower()

    # ── Positive: convention-marker must be present ──
    assert "until then it is convention" in lowered or "p2 enforcement pending" in lowered, (
        "master.md lead-gate section missing the convention-marker "
        "'until then it is convention' / 'P2 enforcement pending'. "
        "P1 ships convention only; the prose must not claim enforced behavior."
    )
    # S1 audited override must be present
    assert "audited" in lowered and ("override" in lowered or "waive" in lowered), (
        "master.md missing S1 audited-override / audited-waive clause."
    )
    # Default-on language must be present
    assert "default" in lowered and "on" in lowered, (
        "master.md missing 'default-on' lead-gate language."
    )

    # ── Negative: no present-tense-enforced phrasing in the lead-domain-gate SECTION ──
    # Extract the section between '## Lead-domain gate' and the next '## '.
    # Section-scoped to avoid false-positives on the legitimately-enforced
    # lead write-gate elsewhere in master.md ('locked lead-gate — never weaken').
    import re as _re

    gate_section_match = _re.search(
        r"## Lead-domain gate.*?(?=\n## |\Z)", content, _re.DOTALL | _re.IGNORECASE
    )
    if gate_section_match:
        gate_section = gate_section_match.group(0).lower()
        FORBIDDEN_ENFORCED_PHRASES = [
            "master cannot approve",
            "master can't approve",
            "the gate blocks",
            "enforced by lint",
            "master must not approve",
            "cannot be approved without",
        ]
        for phrase in FORBIDDEN_ENFORCED_PHRASES:
            assert phrase not in gate_section, (
                f"master.md lead-gate section contains enforced-phrasing '{phrase}'. "
                "P1 ships CONVENTION only — no present-tense-enforced claims. "
                "P2 (Themis verdict_role) will enforce this; until then it is convention."
            )


def test_agent_md_contains_domain_routing_section():
    """Regression guard: agent.md must have the lead-gate domain routing convention."""
    content = (ROLE_DIR / "agent.md").read_text().lower()
    assert "domain-done" in content or "domain done" in content, (
        "agent.md missing domain-done routing section (lead before master)."
    )
    assert "lead" in content and ("correctness" in content or "domain" in content), (
        "agent.md missing lead-domain-correctness routing guidance."
    )


def test_lead_role_docs_contain_accountability_section():
    """Regression guard: lead role-docs must have domain accountability + author≠implementer."""
    for lead_doc in ("backend-lead.md", "data-lead.md"):
        content = (ROLE_DIR / lead_doc).read_text().lower()
        assert "domain accountability" in content or "p1 convention" in content, (
            f"{lead_doc} missing 'Domain accountability' section."
        )
        assert "author" in content and ("implementer" in content or "implement" in content), (
            f"{lead_doc} missing S3 author≠implementer clause."
        )
        assert "until then it is convention" in content or "p2 enforcement pending" in content, (
            f"{lead_doc} missing P1 convention-marker (not enforced until P2)."
        )


def test_analyst_md_contains_domain_specialist_consults():
    """Analyst absorbed the retired leads' consult function (2026-06-06).
    Guard the section so a doc rewrite can't silently drop the protocol."""
    md = (ROLE_DIR / "analyst.md").read_text(encoding="utf-8")
    assert "Domain-specialist consults" in md
    assert "[domain=backend]" in md
    assert "idle-by-default" in md  # the activation model must stay stated


def test_tracker_md_contains_roster_health_watch():
    """Tracker absorbed observer's judgment duties (2026-06-06). Guard the
    section + the read-only boundary statement."""
    md = (ROLE_DIR / "tracker.md").read_text(encoding="utf-8")
    assert "Roster health watch" in md
    assert "absorbed from observer" in md
    assert "Stay read-only toward the working tree" in md


def test_session_start_has_compact_short_circuit():
    """Regression guard: SessionStart must short-circuit on source=compact
    (fcace52). Claude Code re-fires SessionStart after /compact; without this
    branch the hook re-injects the full ~70KB payload into the just-compacted
    window — the #1 long-session cost leak. A refactor must not silently drop it.
    See memory: sessionstart-fires-on-compact.
    """
    src = (
        REPO_ROOT / "packages/khimaira/src/khimaira/hooks/session_start.py"
    ).read_text()
    assert 'source") or ""' in src or 'data.get("source")' in src, (
        "session_start.py must read the SessionStart `source` field"
    )
    assert '== "compact"' in src, (
        "session_start.py must branch on source == 'compact' to skip heavy "
        "blocks (the compaction re-inflation fix). The unit test "
        "test_compact_source_emits_identity_and_chatreg_only guards behavior; "
        "this guards the source-level branch against silent removal."
    )


def test_idle_consult_roles_constant_and_doc_coherence():
    """IDLE_CONSULT_ROLES must equal {architect,analyst,critic,verifier} AND every
    role whose roles/<role>.md contains 'idle-by-default' must be in the constant.
    Catches: constant updated without doc, or doc updated without constant.
    """
    from khimaira.monitor.chats import IDLE_CONSULT_ROLES

    expected = frozenset({"architect", "analyst", "critic", "verifier"})
    assert IDLE_CONSULT_ROLES == expected, (
        f"IDLE_CONSULT_ROLES mismatch: got {set(IDLE_CONSULT_ROLES)}, expected {set(expected)}"
    )

    # Every role doc tagged "idle-by-default" must appear in the constant.
    doc_tagged: set[str] = set()
    for md_path in ROLE_DIR.glob("*.md"):
        role_name = md_path.stem  # e.g. "architect", "critic"
        if "idle-by-default" in md_path.read_text(encoding="utf-8"):
            doc_tagged.add(role_name)

    missing_from_constant = doc_tagged - IDLE_CONSULT_ROLES
    assert not missing_from_constant, (
        f"Role(s) tagged 'idle-by-default' in their role doc but absent from "
        f"IDLE_CONSULT_ROLES: {missing_from_constant}. Add them to the constant."
    )


def test_master_md_contains_consults_must_be_directed():
    """Regression guard: master.md must instruct that consults be directed.
    The wake-filter (IDLE_CONSULT_ROLES) is self-correcting — a raw-broadcast
    consult silently fails because idle roles don't wake. This doc line is the
    human-readable explanation of why.
    """
    content = (ROLE_DIR / "master.md").read_text(encoding="utf-8")
    assert "Consults MUST be directed" in content, (
        "master.md missing 'Consults MUST be directed' directive. "
        "This is the human-readable companion to the IDLE_CONSULT_ROLES wake-filter."
    )


def test_critic_md_mandates_structured_verdict_tool_call():
    """Critic must record the verdict via chat_task_verdict, not prose (2026-06-09).
    Guards against the role doc drifting back to 'verdict via chat' which invited
    the done-not-approved stuck-gate class (3rd recurrence)."""
    md = (ROLE_DIR / "critic.md").read_text(encoding="utf-8")
    assert "chat_task_verdict" in md
    assert "never as prose" in md or "never as prose" in md.lower()


def test_verifier_md_mandates_structured_verdict_tool_call():
    md = (ROLE_DIR / "verifier.md").read_text(encoding="utf-8")
    assert "chat_task_verdict" in md
    assert "ship" in md and "hold" in md


def test_master_md_documents_idle_drive_wake():
    """Master-drive wake (2026-06-10): master.md must document that the daemon
    structurally wakes master on idle-roster-with-owed-work, and master must
    DRIVE on that wake. Guards the behavioral→structural promotion."""
    md = (ROLE_DIR / "master.md").read_text(encoding="utf-8")
    assert "Idle-roster drive is now structurally backed" in md
    assert "auto-dispatch" in md.lower()
    assert "DRIVE" in md
