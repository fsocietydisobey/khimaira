"""Node factories for all CHIMERA patterns."""

# SPR-4 (Sequential Phase Runner)
from chimera.nodes.balanced.arbitrator import build_arbitrator_node
from chimera.nodes.balanced.compliance import build_compliance_node
from chimera.nodes.balanced.integration_gate import build_integration_gate_node
from chimera.nodes.balanced.retry_controller import build_retry_controller_node
from chimera.nodes.balanced.scope_analyzer import build_scope_analyzer_node

# TFB (Tri-Force Balancer)
from chimera.nodes.balanced.stress_tester import build_stress_tester_node
from chimera.nodes.components.enforcer import build_component_enforcer_node

# ACL (Atomic Component Library)
from chimera.nodes.components.scanner import build_component_scanner_node
from chimera.nodes.components.validator import build_component_validator_node
from chimera.nodes.deadcode.reaper import build_deadcode_reaper_node

# DCE (Dead Code Eliminator)
from chimera.nodes.deadcode.seeker import build_deadcode_seeker_node
from chimera.nodes.deadcode.shatterer import build_deadcode_shatterer_node

# Shared
from chimera.nodes.gemini_assist import build_gemini_assist_node
from chimera.nodes.human_review import build_human_review_node

# HVD (Hypervisor Daemon)
from chimera.nodes.hypervisor_dispatcher import build_hypervisor_dispatcher_node
from chimera.nodes.pipeline.architect import build_architect_node
from chimera.nodes.pipeline.critic import build_critic_node
from chimera.nodes.pipeline.implement import build_implement_node
from chimera.nodes.pipeline.research import build_research_node
from chimera.nodes.refiner.classifier import build_classifier_node

# CLR (Closed-Loop Refiner)
from chimera.nodes.refiner.health_scanner import build_health_scanner_node
from chimera.nodes.supervisor import build_supervisor_node
from chimera.nodes.swarm.aggregator import build_swarm_aggregator_node

# PDE (Parallel Dispatch Engine)
from chimera.nodes.swarm.task_decomposer import build_swarm_decomposer_node
from chimera.nodes.swarm.worker import build_swarm_worker_node
from chimera.nodes.toolbuilder.forge import build_toolbuilder_forge_node
from chimera.nodes.toolbuilder.friction import build_toolbuilder_friction_node
from chimera.nodes.toolbuilder.pr_creator import build_toolbuilder_pr_creator_node
from chimera.nodes.toolbuilder.proposer import build_toolbuilder_proposer_node

# POB (Proactive Observation Builder)
from chimera.nodes.toolbuilder.watcher import build_toolbuilder_watcher_node
from chimera.nodes.validator import build_validator_node

__all__ = [
    # SPR-4
    "build_architect_node",
    "build_critic_node",
    "build_implement_node",
    "build_research_node",
    # TFB
    "build_stress_tester_node",
    "build_scope_analyzer_node",
    "build_arbitrator_node",
    "build_compliance_node",
    "build_retry_controller_node",
    "build_integration_gate_node",
    # PDE
    "build_swarm_decomposer_node",
    "build_swarm_worker_node",
    "build_swarm_aggregator_node",
    # CLR
    "build_health_scanner_node",
    "build_classifier_node",
    # HVD
    "build_hypervisor_dispatcher_node",
    # ACL
    "build_component_scanner_node",
    "build_component_validator_node",
    "build_component_enforcer_node",
    # DCE
    "build_deadcode_seeker_node",
    "build_deadcode_shatterer_node",
    "build_deadcode_reaper_node",
    # POB
    "build_toolbuilder_watcher_node",
    "build_toolbuilder_friction_node",
    "build_toolbuilder_proposer_node",
    "build_toolbuilder_forge_node",
    "build_toolbuilder_pr_creator_node",
    # Shared
    "build_gemini_assist_node",
    "build_human_review_node",
    "build_supervisor_node",
    "build_validator_node",
]
