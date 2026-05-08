"""Orchestrator state — shared TypedDict flowing through the LangGraph graph.

Each node reads from and writes to this state. The `messages` field uses
LangGraph's `add_messages` reducer so chat history accumulates across nodes.
Domain output fields are last-writer-wins (str) for easy consumption.
The `output_versions` list uses an append reducer to accumulate every version
across attempts and timelines — useful for comparing rewound branches.
"""

import operator
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _node_failure_reducer(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """Reducer for node_failure field.

    - If b is not None (node explicitly set it), use b — even if it's {}
    - Otherwise keep a
    This lets nodes clear failures by returning node_failure: {}
    """
    if b is not None:
        return b
    return a or {}


class OrchestratorState(TypedDict, total=False):
    """State that flows through the orchestrator graph.

    Required fields (set at graph entry):
        task: The user's task description.

    Optional fields (set by nodes as they execute):
        context: User-provided context.
        messages: Chat message history — uses add_messages reducer.

    Domain outputs (set by domain nodes):
        research_findings: Markdown output from the research node.
        architecture_plan: Markdown output from the architect node.
        implementation_result: Output from the implement node.

    Supervisor fields (v0.4 — dynamic supervisor pattern):
        next_node: Which node the supervisor wants to call next.
        supervisor_rationale: Why the supervisor chose that node.
        supervisor_instructions: Instructions for the next node.
        history: List of step summaries (supervisor decisions, node results).
        node_calls: Dict tracking how many times each node has been called.

    Fan-out fields (v0.5 — parallel research):
        parallel_tasks: List of sub-tasks for fan-out. Each dict has 'topic'
            and 'instructions'. Set by supervisor, cleared by merge_research.
        parallel_task_topic: Topic label for a single Send() branch. Set in
            the Send payload, read by the research node.

    Human-in-the-loop fields (v0.5 — HITL):
        human_review_status: "pending", "approved", or "rejected".
            Set by human_review node, updated on resume.
        human_feedback: Feedback from the human reviewer. Empty if approved
            without comment. Passed to the next node on resume.

    Output versioning:
        output_versions: Append-only list of every domain output across
            attempts/timelines. Each entry is a dict with node, attempt,
            and content. Uses operator.add reducer (like messages).

    Validation fields:
        validation_score: 0.0-1.0 quality score from the validator.
        validation_feedback: Actionable feedback if score is low.

    Error recovery:
        node_failure: When a domain node fails (CLI error or exception), set to
            a dict with "node", "error", "attempt". Cleared (set to {}) when
            the node succeeds. Supervisor uses this to decide retry/skip/finish.
            Uses a reducer so parallel research branches can each write without conflict.
    """

    # --- Input ---
    task: str
    context: str

    # --- Chat history ---
    messages: Annotated[list[AnyMessage], add_messages]

    # --- Domain outputs (latest — last-writer-wins) ---
    research_findings: str
    architecture_plan: str
    implementation_result: str

    # --- Output versioning (append reducer — accumulates across attempts/timelines) ---
    output_versions: Annotated[list[dict[str, Any]], operator.add]

    # --- Supervisor (v0.4) ---
    next_node: str
    supervisor_rationale: str
    supervisor_instructions: str
    history: list[str]
    node_calls: dict[str, int]

    # --- Fan-out (v0.5) ---
    parallel_tasks: list[dict[str, str]]
    parallel_task_topic: str

    # --- Human-in-the-loop (v0.5) ---
    human_review_status: str
    human_feedback: str

    # --- Validation ---
    validation_score: float
    validation_feedback: str

    # --- Error recovery ---
    node_failure: Annotated[dict[str, Any], _node_failure_reducer]

    # --- SPR-4 (phased pipeline) ---
    # Phase routing
    phase: str  # Current phase: "research", "planning", "implementation", "review"
    handoff_type: str  # Routing signal between phases/nodes (e.g. "research_complete", "plan_approved")
    critique: str  # Latest critic feedback for revision loops

    # Approval gates
    plan_approved: bool  # Set by critic when architecture plan passes quality threshold
    human_approved: bool  # Set by human_review node on approval

    # Implementation versioning (append reducer — accumulates across attempts)
    implementation_versions: Annotated[list[dict[str, Any]], operator.add]
    selected_implementation_id: str  # Optional: reviewer/human picks best version

    # Phase step counter (reset at start of each phase)
    phase_step: int  # Current step within the active phase
    max_phase_steps: int  # Max steps allowed for the active phase

    # Implementation subgraph's internal rework-loop counter. Distinct
    # from `phase_step` because the implementation subgraph runs as a
    # single super-step from the parent pipeline's perspective — its
    # internal `implement → stress → scope → arbitrate` loop needs its
    # own counter so the loop can self-bound. Reset on subgraph entry,
    # bumped by arbitrator each loop iteration.
    implementation_loop_step: int  # 0 on entry; bumped per arbitrate iteration

    # Persistent memory
    memory_context: str  # Injected context from past runs

    # --- TFB (balanced forces) ---
    stress_test_verdict: dict[str, Any]  # StressTestVerdict as dict: issues, recommendation
    scope_proposals: list[dict[str, Any]]  # List of Proposal dicts from ScopeAnalyzer
    arbitration_decision: dict[str, Any]  # ArbitrationDecision as dict: accepted, rejected, rationale
    compliance_report: dict[str, Any]  # Compliance report: files_formatted, lint_fixes
    retry_strategy: dict[str, Any]  # RetryStrategy as dict: strategy, modified_instructions
    integration_result: dict[str, Any]  # IntegrationResult as dict: passed, test_results, type_errors
    retry_history: Annotated[list[dict[str, Any]], operator.add]  # Tracks retry attempts for RetryController

    # --- CLR (continuous refinement) ---
    health_report: dict[str, Any]  # HealthReport as dict
    health_score: float  # Current health score (0.0-1.0)
    health_baseline: float  # Score at start of current cycle
    cycle_count: int  # How many refinement cycles completed
    max_cycles: int  # Budget cap on cycles
    consecutive_no_improvement: int  # For convergence detection
    requires_restart: bool  # Self-modification detected
    refiner_action: str  # "fix" | "refactor" | "feature" | "idle"
    refiner_task: str  # Task description for SPR-4

    # --- PDE (parallel dispatch) ---
    swarm_manifest: dict[str, Any]  # TaskManifest as dict
    swarm_results: Annotated[list[dict[str, Any]], operator.add]  # Per-agent results (append)
    swarm_outcome: str  # "success" | "failed" | "partial"
    swarm_test_output: str  # Test output after merge
    swarm_budget: dict[str, Any]  # Budget config
    swarm_cost_estimate: float  # Running cost estimate

    # --- PDE-F (graduated dispatch) ---
    current_generation: int  # Which generation is being dispatched
    generation_results: Annotated[list[dict[str, Any]], operator.add]  # Per-generation results
    dispatch_mode: str  # "flat" | "pdef"

    # --- HVD (meta-orchestrator) ---
    active_entities: list[dict[str, Any]]  # Running entities tracked by HVD
    dispatch_decision: dict[str, Any]  # DispatchDecision as dict
    directive_result: dict[str, Any]  # DirectiveResult as dict
    global_budget: dict[str, Any]  # GlobalBudget as dict
    hypervisor_cycle: int  # HVD's own cycle count

    # --- ACL (Atomic Component Library) ---
    component_scan_result: dict[str, Any]  # Scanner output: components, violations
    component_validation_report: dict[str, Any]  # Validator output: isolation, pairs, scenarios
    component_enforcement_result: dict[str, Any]  # Enforcer output: passed, violations

    # --- DCE (Dead Code Eliminator) ---
    deadcode_shadow_path: str  # Path to shadow worktree
    deadcode_targets: dict[str, Any]  # Seeker output: target map with risk levels
    deadcode_split_proposals: list[dict[str, Any]]  # Shatterer output: file split proposals
    deadcode_reap_result: dict[str, Any]  # Reaper output: deleted, reverted, lines removed

    # --- POB (Proactive Observation Builder) ---
    toolbuilder_signals: list[dict[str, Any]]  # Watcher output: behavioral signals
    toolbuilder_friction_points: list[dict[str, Any]]  # Friction analyzer output
    toolbuilder_tool_spec: dict[str, Any] | None  # Proposer output: tool specification
    toolbuilder_forge_result: dict[str, Any] | None  # Forge output: build result
    toolbuilder_pr_result: dict[str, Any] | None  # PR creator output
