"""Pydantic models for the auto-generated project metadata cache.

Schema version 1. All fields except `schema_version` and `project_name`
are optional — a brand-new cache from an LLM scan that can't classify a
graph still validates as a usable (if minimal) document.

The schema is intentionally narrow: only fields that DRIVE diagram
rendering. We don't store anything purely descriptive; if it's not
going to change a pixel on screen, it doesn't belong here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Roles drive diagram styling. Keep this list in sync with the classDef
# palette in api/topology.py.
NodeRole = Literal[
    "entry",       # __start__-like — gold pill
    "exit",        # __end__-like — green pill
    "router",      # decision/dispatch — purple diamond
    "gate",        # HITL / approval — amber rectangle
    "critic",      # validation / quality check — red rectangle
    "synthesis",   # aggregation / commit / merge — green rectangle
    "executor",    # main work — slate rectangle (default)
]

GraphRole = Literal[
    "orchestrator",   # top-level — invokes one or more subgraphs
    "subgraph",       # invoked by an orchestrator
    "leaf",           # standalone, neither invokes nor is invoked
]

LayoutDirection = Literal["TB", "BT", "LR", "RL"]


class NodeMetadata(BaseModel):
    """Per-node enrichment. None = AST default applies."""

    role: NodeRole | None = None
    label: str | None = None        # display name (overrides node id)
    summary: str | None = None      # one-line description, used in tooltips


class GraphMetadata(BaseModel):
    """Per-graph enrichment."""

    role: GraphRole | None = None
    label: str | None = None
    summary: str | None = None
    layout: LayoutDirection = "LR"
    # Map from this graph's node name → the name of the graph it invokes.
    # Drives inter-graph subgraph edges in the unified view.
    invokes: dict[str, str] = Field(default_factory=dict)
    # Per-node enrichment. Missing entries fall back to defaults.
    nodes: dict[str, NodeMetadata] = Field(default_factory=dict)


class ThreadIdPattern(BaseModel):
    """One regex rule for parsing a project's thread_ids into grouping fields.

    The regex must use named groups: `scope_id` (required), `stage`
    (optional), `stage_detail` (optional). When `scope_kind` is not a
    capture group, the static value below is used. Multiple patterns
    are tried in order; first match wins.
    """

    pattern: str                      # Python regex with named groups
    scope_kind: str                   # static label when not captured (e.g. "deliverable")
    stage: str | None = None          # static stage label if regex doesn't capture it


class ThreadGrouping(BaseModel):
    """Project-specific configuration for parsing thread_ids."""

    # Display label for the scope group header (e.g. "Deliverable", "Run", "Chain")
    scope_label: str = "Run"
    # Patterns tried in order. Empty list = use the heuristic fallback.
    patterns: list[ThreadIdPattern] = Field(default_factory=list)
    # Sample thread_ids Claude inspected during the scan. Stored for
    # debugging when patterns produce wrong results — the user can read
    # the cache file to see what Claude saw.
    examples: list[str] = Field(default_factory=list)


class NodeStats(BaseModel):
    """Empirical statistics for one node, derived by the observation
    collector from accumulated checkpoint history."""

    visits: int = 0
    duration_p50: float = 0.0
    duration_p95: float = 0.0
    duration_max: float = 0.0


class GraphObservations(BaseModel):
    """Per-graph runtime observations. The current collector aggregates
    everything under a single `_aggregate` bucket; future iterations may
    split per-graph if node-name collisions become a problem."""

    nodes: dict[str, NodeStats] = Field(default_factory=dict)
    # How often each node was seen as the latest current_node when its
    # thread reached a settled state. Strong empirical signal for
    # "where this graph tends to end."
    end_node_counts: dict[str, int] = Field(default_factory=dict)


class RuntimeObservations(BaseModel):
    """Cumulative runtime observations for a project. Persisted in a
    separate file from the LLM-derived metadata so the two cadences
    don't race."""

    last_collected_at: str
    samples_seen: int = 0
    graphs: dict[str, GraphObservations] = Field(default_factory=dict)


RunClusterSourceField = Literal["thread_id", "scope_id", "stage", "stage_detail"]


class RunClustering(BaseModel):
    """How sister threads should be clustered into 'runs' — a logical
    execution pass that may span multiple threads.

    Applied AFTER `thread_grouping` has parsed each thread_id into
    fields. The cluster key is extracted by running `pattern` against
    the value of `source_field`; the FIRST capture group becomes the
    key. Threads with matching keys cluster together. Threads where
    the pattern doesn't match fall into time-proximity grouping
    bounded by `time_window_seconds`.

    Examples:
      - jeevy `deliverable:<uuid>:digestion:<run-uuid>` — sister threads
        share a trailing UUID in `stage_detail`:
          source_field: stage_detail
          pattern: "([0-9a-f-]{36})$"
      - apps using serial run numbers like `pipeline:run-42`:
          source_field: stage_detail
          pattern: "run-(\\d+)"
      - apps with no shared run id — purely time-bucketed:
          source_field: thread_id
          pattern: null

    The `pattern` MUST use JavaScript-compatible regex syntax (the
    frontend executes it via `RegExp`). First capture group is the
    cluster key. Use `null` to skip pattern matching and rely solely
    on time proximity.
    """

    source_field: RunClusterSourceField = "stage_detail"
    pattern: str | None = None
    time_window_seconds: int = 300
    # Display label for a cluster row in the sidebar (e.g. "Run",
    # "Pipeline", "Cycle"). Distinct from `ThreadGrouping.scope_label`
    # which labels the higher-level scope group.
    run_label: str = "Run"


class ProjectMetadata(BaseModel):
    """The full cache document for one project."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    project_name: str
    project_path: str
    # ISO-8601 UTC timestamp of when this cache was written.
    generated_at: str
    # Newest mtime across the project's graph source files at scan time.
    # Used as the cache invalidation watermark.
    source_mtime_max: float = 0.0
    # Free-form architecture summary (Gemini's headline take on the project).
    summary: str = ""
    # Per-graph enrichment, keyed by the same graph_name the AST walker emits.
    graphs: dict[str, GraphMetadata] = Field(default_factory=dict)
    # Project-specific thread_id parsing rules. Optional — when absent
    # the backend falls back to the generic heuristic in
    # discovery/thread_grouping.py.
    thread_grouping: ThreadGrouping | None = None
    # Project-specific run-clustering rules. Optional — when absent
    # the frontend uses a trailing-UUID + time-proximity heuristic.
    run_clustering: RunClustering | None = None
    # How long a node can legitimately run without writing a new
    # checkpoint before the dashboard considers the thread idle.
    # Default 300s (5min) when omitted. Apps with slow LLM-bearing
    # nodes (e.g. chimera's pipeline does 8min Claude calls) should
    # bump this up; apps with fast nodes can lower it for tighter
    # idle detection. The LLM scan derives this by inspecting node
    # bodies — looking for LLM/HTTP/subprocess calls and their typical
    # latency.
    running_threshold_seconds: int | None = None
