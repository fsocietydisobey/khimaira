"""Tests for the khimaira-graph MCP tool family (kg_* in monitor_tools).

These are thin async wrappers over the daemon's generic graph proxy
(/api/graph/<project>...). The daemon proxies to a project's KG adapter; the
tools format the generic contract for an agent debugging an LLM-extracted
graph. We mock `_get` (the daemon HTTP layer, already covered by
test_monitor_tools) and assert:

  1. Happy path — the formatted output carries the real data faithfully
     (all facts / edges / provenance / triples — these are debug tools, so
     completeness matters more than brevity).
  2. The `{"data": ...}` envelope is unwrapped.
  3. A daemon error string passes through verbatim (no crash, no masking).
  4. Empty graph / empty schema produce a clear message, not a stack trace.
  5. kg_search ranking: exact label → prefix → label substring → id substring.
  6. scope passes through into the request path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from khimaira.server import monitor_tools as mt


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# kg_graph
# ---------------------------------------------------------------------------


def test_kg_graph_happy_counts_and_histograms():
    payload = {
        "data": {
            "nodes": [
                {"id": "n1", "type": "job", "label": "JOB-1", "badge": 3},
                {"id": "n2", "type": "task", "label": "Cut steel"},
                {"id": "n3", "type": "task", "label": "Weld"},
            ],
            "edges": [
                {"id": "e1", "from": "n2", "to": "n1", "type": "belongs-to"},
                {"id": "e2", "from": "n3", "to": "n1", "type": "belongs-to"},
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_graph("jeevy", "shop:10"))
    assert "3 nodes, 2 edges" in out
    assert "task×2" in out and "job×1" in out  # node-type histogram
    assert "belongs-to×2" in out  # link-type histogram
    assert "`n1`" in out and "JOB-1" in out  # node sample carries id + label


def test_kg_graph_node_cap_truncates_sample_not_counts():
    nodes = [{"id": f"n{i}", "type": "task", "label": f"T{i}"} for i in range(50)]
    payload = {"data": {"nodes": nodes, "edges": []}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_graph("jeevy", "shop:10", node_cap=5))
    assert "50 nodes" in out  # count reflects full graph
    assert "45 more" in out  # sample capped at 5


def test_kg_graph_sample_is_type_stratified():
    # 60 'task' nodes then 2 'user' nodes. A naive first-N sample at cap=10 would
    # only ever show tasks (users sit at positions 60-61) — the exact bug that hid
    # shop:10's user nodes. The stratified sample must surface the leaf type.
    nodes = [{"id": f"t{i}", "type": "task", "label": f"T{i}"} for i in range(60)]
    nodes += [
        {"id": "u1", "type": "user", "label": "Alice"},
        {"id": "u2", "type": "user", "label": "Bob"},
    ]
    payload = {"data": {"nodes": nodes, "edges": []}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_graph("jeevy", "shop:10", node_cap=10))
    assert "62 nodes" in out  # counts reflect the full graph
    assert "(user)" in out  # leaf type appears in the sample (was invisible pre-fix)
    assert "type-stratified" in out  # header advertises the new behavior


def test_kg_graph_empty():
    with patch.object(mt, "_get", return_value={"data": {"nodes": [], "edges": []}}):
        out = _run(mt.kg_graph("jeevy", "shop:99"))
    assert "empty" in out.lower()


def test_kg_graph_error_passthrough():
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 404: no adapter"):
        out = _run(mt.kg_graph("nope"))
    assert "HTTP 404" in out


# ---------------------------------------------------------------------------
# kg_node — the keystone surface
# ---------------------------------------------------------------------------


def test_kg_node_full_detail():
    payload = {
        "data": {
            "id": "n1",
            "type": "task",
            "label": "Procure angle iron",
            "badge": 4,
            "currentFacts": [
                {"label": "status", "value": "open", "meta": {"confidence": "99%"}},
                {"label": "assignee", "value": "jsmith"},
            ],
            "historyFacts": [
                {"label": "status", "value": "draft", "deprecated": True},
            ],
            "edgesFrom": [
                {"id": "e1", "from": "n1", "to": "ws1", "type": "belongs-to", "weight": 0.97},
            ],
            "edgesTo": [
                {"id": "e2", "from": "u1", "to": "n1", "type": "created-by", "weight": 1.0},
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_node("jeevy", "n1", "shop:10"))
    # Header
    assert "Procure angle iron" in out and "task" in out and "n1" in out
    # Current facts with meta chip
    assert "status = 'open'" in out and "confidence=99%" in out
    assert "assignee = 'jsmith'" in out
    # History rendered separately
    assert "History" in out and "draft" in out
    # Edges carry the edge id (so agent can call kg_edge) + target + weight
    assert "belongs-to" in out and "`ws1`" in out and "edge=`e1`" in out
    assert "created-by" in out and "`u1`" in out and "edge=`e2`" in out


def test_kg_node_no_facts():
    payload = {
        "data": {
            "id": "n1",
            "type": "shop",
            "label": "Shop",
            "currentFacts": [],
            "historyFacts": [],
            "edgesFrom": [],
            "edgesTo": [],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_node("jeevy", "n1"))
    assert "Current facts (0)" in out
    assert "(none)" in out


def test_kg_node_error_passthrough():
    with patch.object(mt, "_get", return_value="daemon is not running"):
        out = _run(mt.kg_node("jeevy", "n1"))
    assert "daemon is not running" in out


# ---------------------------------------------------------------------------
# kg_edge — provenance
# ---------------------------------------------------------------------------


def test_kg_edge_provenance():
    payload = {
        "data": {
            "id": "e1",
            "type": "belongs-to",
            "from": "n2",
            "to": "n1",
            "weight": 0.97,
            "meta": {
                "match_method": "exact",
                "source_doc": "quote-may.pdf",
                "page": 3,
            },
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edge("jeevy", "e1", "shop:10"))
    assert "belongs-to" in out and "`n2`" in out and "`n1`" in out
    assert "weight=0.970" in out
    assert "match_method: exact" in out
    assert "source_doc: quote-may.pdf" in out
    assert "page: 3" in out


def test_kg_edge_no_meta():
    payload = {"data": {"id": "e1", "type": "rel", "from": "a", "to": "b"}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edge("jeevy", "e1"))
    assert "none recorded" in out.lower()


# ---------------------------------------------------------------------------
# kg_schema — the structural-gap finder
# ---------------------------------------------------------------------------


def test_kg_schema_meta_graph():
    payload = {
        "data": {
            "nodeTypes": ["job", "part", "task"],
            "linkTypes": ["belongs-to", "for-part"],
            "triples": [
                {"fromType": "task", "linkType": "belongs-to", "toType": "job", "count": 2},
                {"fromType": "part", "linkType": "for-part", "toType": "task", "count": 1},
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_schema("jeevy", "shop:10"))
    assert "job, part, task" in out
    assert "belongs-to, for-part" in out
    assert "task -[belongs-to]-> job  × 2" in out
    assert "part -[for-part]-> task  × 1" in out


def test_kg_schema_empty():
    with patch.object(
        mt, "_get", return_value={"data": {"nodeTypes": [], "linkTypes": [], "triples": []}}
    ):
        out = _run(mt.kg_schema("jeevy", "shop:99"))
    assert "empty" in out.lower()


# ---------------------------------------------------------------------------
# kg_search — id resolution + ranking
# ---------------------------------------------------------------------------


def _search_payload():
    return {
        "data": {
            "nodes": [
                {"id": "n1", "type": "task", "label": "weld frame"},
                {"id": "n2", "type": "task", "label": "weld"},  # exact
                {"id": "n3", "type": "task", "label": "reweld joint"},  # substring
                {"id": "weld-x", "type": "part", "label": "Bracket"},  # id substring
                {"id": "n5", "type": "task", "label": "cut steel"},  # no match
            ],
            "edges": [],
        }
    }


def test_kg_search_ranks_exact_first():
    with patch.object(mt, "_get", return_value=_search_payload()):
        out = _run(mt.kg_search("jeevy", "weld", "shop:10"))
    assert "4 match(es)" in out  # excludes "cut steel"
    # Exact label "weld" (n2) must rank above prefix "weld frame" (n1),
    # which ranks above substring "reweld joint" (n3) and id-match (weld-x).
    pos_n2 = out.index("`n2`")
    pos_n1 = out.index("`n1`")
    pos_n3 = out.index("`n3`")
    pos_idmatch = out.index("`weld-x`")
    assert pos_n2 < pos_n1 < pos_n3
    assert pos_n3 < pos_idmatch


def test_kg_search_no_match():
    with patch.object(mt, "_get", return_value=_search_payload()):
        out = _run(mt.kg_search("jeevy", "zzz-nothing"))
    assert "no nodes" in out.lower()


def test_kg_search_empty_query_rejected():
    with patch.object(mt, "_get", return_value=_search_payload()) as g:
        out = _run(mt.kg_search("jeevy", "   "))
    assert "non-empty" in out
    g.assert_not_called()  # short-circuits before hitting the daemon


def test_kg_search_limit():
    nodes = [{"id": f"n{i}", "type": "task", "label": f"weld {i}"} for i in range(30)]
    with patch.object(mt, "_get", return_value={"data": {"nodes": nodes, "edges": []}}):
        out = _run(mt.kg_search("jeevy", "weld", limit=5))
    assert "30 match(es)" in out
    assert "25 more" in out


# ---------------------------------------------------------------------------
# Envelope handling + scope passthrough (shared behavior)
# ---------------------------------------------------------------------------


def test_unwrap_handles_bare_payload_without_data_key():
    """A future adapter that returns bare JSON (no `data` wrapper) still works."""
    bare = {"nodes": [{"id": "n1", "type": "t", "label": "L"}], "edges": []}
    with patch.object(mt, "_get", return_value=bare):
        out = _run(mt.kg_graph("other"))
    assert "1 nodes" in out


def test_scope_passes_through_into_request_path():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"nodes": [], "edges": []}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_graph("jeevy", "shop:10"))
    assert "scope=shop%3A10" in captured["path"]  # url-encoded shop:10


def test_no_scope_omits_query_string():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"nodes": [], "edges": []}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_graph("jeevy"))
    assert "?scope=" not in captured["path"]
    assert captured["path"].endswith("/api/graph/jeevy")


# ---------------------------------------------------------------------------
# kg_view_url — the screenshot bridge (builds a framed deep-link; no daemon hit)
# ---------------------------------------------------------------------------


def test_kg_view_url_minimal():
    out = _run(mt.kg_view_url("backend", "shop:10"))
    assert "/backend/kg?scope=shop%3A10" in out
    assert "specter_take_screenshot" in out  # includes the capture recipe


def test_kg_view_url_all_framing_params():
    out = _run(
        mt.kg_view_url(
            "backend",
            "shop:10",
            select_node="abc-123",
            isolate=True,
            edge_mode="confidence",
            conf=0.7,
            zoom=0.15,
        )
    )
    assert "selectNode=abc-123" in out
    assert "isolate=1" in out
    assert "edgeMode=confidence" in out
    assert "conf=0.7" in out
    assert "zoom=0.15" in out


def test_kg_view_url_drops_invalid_conf_and_defaults():
    # conf only accepts the UI's exact 0.9 / 0.7; anything else is dropped.
    out = _run(mt.kg_view_url("backend", "shop:10", conf=0.5))
    # Check the URL line specifically (the help text below it names params).
    url_line = next(ln for ln in out.splitlines() if ln.startswith("http"))
    assert "conf=" not in url_line
    assert "isolate=" not in url_line
    assert "edgeMode=" not in url_line
    assert "selectNode=" not in url_line
    assert url_line.endswith("/backend/kg?scope=shop%3A10")


# ---------------------------------------------------------------------------
# Phase 3 aggregate tools — kg_health / kg_coverage / kg_edges_audit + since
# ---------------------------------------------------------------------------


def test_kg_health_distinguishes_orphan_from_disconnected():
    payload = {
        "data": {
            "totals": {"nodes": 5719, "edges": 9970, "orphanNodes": 1814, "danglingEdges": 3},
            "nodeTypes": [
                {"type": "bom-line", "count": 3422, "orphanCount": 1814},
                {"type": "job", "count": 276, "orphanCount": 0},
            ],
            "edgeTypes": [{"type": "part-of", "count": 3399}],
            "containment": [
                # jobs: HAVE edges but 172 lack an upward parent link.
                {
                    "childType": "job",
                    "linkType": "belongs-to",
                    "parentType": "workstream",
                    "withParent": 104,
                    "total": 276,
                    "ratio": 104 / 276,
                },
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_health("backend", "shop:10"))
    # Orphan (degree-0) total surfaced…
    assert "1814 orphan (degree-0)" in out
    assert "3 dangling" in out
    # …and containment disconnected (≠ degree-0) is a SEPARATE, labeled metric.
    assert "Containment" in out and "≠ degree-0" in out
    assert "172 DISCONNECTED" in out  # 276 - 104
    assert "104/276 have a parent" in out


def test_kg_health_error_passthrough():
    with patch.object(mt, "_get", return_value="daemon is not running"):
        out = _run(mt.kg_health("backend"))
    assert "daemon is not running" in out


def test_kg_coverage_ratio_and_under_projection_flag():
    payload = {
        "data": {
            "entities": [
                {"entity": "user", "relationalCount": 46, "kgCount": 4, "ratio": 4 / 46},
                {"entity": "job", "relationalCount": 276, "kgCount": 276, "ratio": 1.0},
            ]
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_coverage("backend", "shop:10"))
    # Worst coverage first → user before job.
    assert out.index("user:") < out.index("job:")
    assert "user: 4/46 nodes (ratio 0.09)" in out and "under-projected" in out
    assert "job: 276/276 nodes (ratio 1.00)" in out
    assert "under-projected" not in out.split("job:")[1]  # job not flagged


def test_kg_edges_audit_buckets_isolate_one_and_no_silent_truncation():
    payload = {
        "data": {
            "matchMethods": [{"method": "normalized", "count": 9970}],
            # 1.0 is its OWN bucket (lo==hi) — distinct from anything <1.0.
            "confidenceBuckets": [
                {"lo": 0.0, "hi": 0.7, "count": 0},
                {"lo": 0.7, "hi": 0.9, "count": 0},
                {"lo": 0.9, "hi": 1.0, "count": 0},
                {"lo": 1.0, "hi": 1.0, "count": 9970},
            ],
            "suspect": [],
            "suspectTotal": 0,
            "truncated": False,
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edges_audit("backend", "shop:10"))
    assert "normalized ×9970" in out
    assert "{1.0}: 9970" in out  # exactly-1.0 isolated, not smeared into [0.9,1.0)
    assert "[0.9,1.0): 0" in out
    assert "0 total" in out and "none" in out.lower()


def test_kg_edges_audit_surfaces_truncation():
    suspect = [
        {"id": f"e{i}", "type": "rel", "from": "a", "to": "b", "weight": 0.5} for i in range(5)
    ]
    payload = {
        "data": {
            "matchMethods": [],
            "confidenceBuckets": [],
            "suspect": suspect,
            "suspectTotal": 47,
            "truncated": True,
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edges_audit("backend", "shop:10"))
    assert "47 total" in out
    assert "showing 5 of 47" in out and "TRUNCATED" in out


def test_since_forwarded_into_request_path():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"totals": {}, "nodeTypes": [], "edgeTypes": [], "containment": []}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_health("backend", "shop:10", since="2026-06-01T00:00:00Z"))
    assert "since=2026-06-01T00%3A00%3A00Z" in captured["path"]
    assert "/health?" in captured["path"]


# ---------------------------------------------------------------------------
# SCAFFOLDING (2026-07-13, task-f4220ba84f33) — kg_subgraph/kg_anomalies/
# kg_edges_filter/kg_path. Their daemon-side jeevy endpoints don't exist yet;
# these tests verify only the khimaira-side plumbing (request path built
# correctly, response shell parsed/formatted, error/empty passthrough) against
# an ASSUMED response shape — NOT a confirmed adapter contract. Once
# griffin-0's real endpoints land, re-verify these against a live response
# before trusting them (per the codebase's own audit-grade-evidence rule).
# ---------------------------------------------------------------------------


def test_kg_subgraph_happy():
    payload = {
        "data": {
            "nodes": [
                {"id": "n1", "type": "job", "label": "JOB-1"},
                {"id": "n2", "type": "task", "label": "Cut steel"},
            ],
            "edges": [{"from": "n2", "to": "n1", "type": "belongs-to", "weight": 1.0}],
            "center": "n1",
            "hops": 2,
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_subgraph("jeevy", "n1", hops=2, scope="shop:10"))
    assert "n1" in out
    assert "2 nodes, 1 edges" in out
    assert "belongs-to" in out


def test_kg_subgraph_requires_node_id():
    out = _run(mt.kg_subgraph("jeevy", ""))
    assert "❌" in out


def test_kg_subgraph_empty():
    with patch.object(mt, "_get", return_value={"data": {"nodes": [], "edges": []}}):
        out = _run(mt.kg_subgraph("jeevy", "n1"))
    assert "no neighborhood" in out.lower()


def test_kg_subgraph_error_passthrough():
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 502: adapter unreachable"):
        out = _run(mt.kg_subgraph("jeevy", "n1"))
    assert "HTTP 502" in out


def test_kg_subgraph_request_path_forwards_hops_and_scope():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"nodes": [], "edges": []}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_subgraph("jeevy", "n1", hops=3, scope="shop:10"))
    assert "/node/n1/subgraph?hops=3&scope=shop%3A10" in captured["path"]


def test_kg_anomalies_happy():
    payload = {
        "data": {
            "orphans": [{"id": "n2", "type": "user", "label": "Ghost"}],
            "danglingEdges": [{"id": "e1", "type": "rel", "from": "a", "to": "missing"}],
            "schemaViolations": [],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_anomalies("jeevy", "shop:10"))
    assert "Orphans (1" in out
    assert "Dangling edges (1" in out
    assert "n2" in out and "Ghost" in out


def test_kg_anomalies_clean_graph():
    payload = {"data": {"orphans": [], "danglingEdges": [], "schemaViolations": []}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_anomalies("jeevy", "shop:10"))
    assert "no anomalies" in out.lower()


def test_kg_anomalies_error_passthrough():
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 502: adapter unreachable"):
        out = _run(mt.kg_anomalies("jeevy"))
    assert "HTTP 502" in out


def test_kg_anomalies_surfaces_truncation():
    payload = {
        "data": {
            "orphans": [{"id": "n1", "type": "user", "label": "X"}],
            "danglingEdges": [],
            "schemaViolations": [],
            "truncated": True,
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_anomalies("jeevy"))
    assert "truncated" in out.lower()


def test_kg_edges_filter_happy():
    payload = {"data": {"edges": [{"id": "e1", "type": "assigned", "from": "a", "to": "b"}], "total": 1}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edges_filter("jeevy", "shop:10", link_type="assigned"))
    assert "1 edge(s) match" in out
    assert "e1" in out


def test_kg_edges_filter_no_matches():
    with patch.object(mt, "_get", return_value={"data": {"edges": [], "total": 0}}):
        out = _run(mt.kg_edges_filter("jeevy", "shop:10", link_type="nonexistent"))
    assert "no edges" in out.lower()


def test_kg_edges_filter_error_passthrough():
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 502: adapter unreachable"):
        out = _run(mt.kg_edges_filter("jeevy"))
    assert "HTTP 502" in out


def test_kg_edges_filter_request_path_forwards_predicates():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"edges": [], "total": 0}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(
            mt.kg_edges_filter(
                "jeevy",
                "shop:10",
                link_type="assigned",
                match_method="fuzzy",
                weight_min=0.1,
                weight_max=0.9,
                status="active",
                source="extractor",
            )
        )
    path = captured["path"]
    assert "link_type=assigned" in path
    assert "match_method=fuzzy" in path
    assert "weight_min=0.1" in path
    assert "weight_max=0.9" in path
    assert "status=active" in path
    assert "source=extractor" in path
    assert "scope=shop%3A10" in path


def test_kg_edges_filter_truncation_message():
    payload = {"data": {"edges": [{"id": "e1", "type": "rel", "from": "a", "to": "b"}], "total": 5}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_edges_filter("jeevy", limit=1))
    assert "4 more" in out


def test_kg_path_happy_found():
    payload = {
        "data": {
            "found": True,
            "hops": 2,
            "path": [
                {"id": "n1", "type": "job", "label": "Job A"},
                {"type": "assigned_to", "from": "n1", "to": "n2"},
                {"id": "n2", "type": "user", "label": "Priya"},
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_path("jeevy", "n1", "n2"))
    assert "path found" in out.lower()
    assert "n1" in out and "n2" in out
    assert "assigned_to" in out


def test_kg_path_not_found():
    payload = {"data": {"found": False, "reason": "no route within 5 hops"}}
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_path("jeevy", "n1", "n99"))
    assert "no path found" in out.lower()
    assert "no route within 5 hops" in out


def test_kg_path_requires_both_nodes():
    out = _run(mt.kg_path("jeevy", "n1", ""))
    assert "❌" in out
    out2 = _run(mt.kg_path("jeevy", "", "n2"))
    assert "❌" in out2


def test_kg_path_rejects_max_hops_over_5():
    out = _run(mt.kg_path("jeevy", "n1", "n2", max_hops=6))
    assert "❌" in out
    assert "5" in out


def test_kg_path_error_passthrough():
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 502: adapter unreachable"):
        out = _run(mt.kg_path("jeevy", "n1", "n2"))
    assert "HTTP 502" in out


def test_kg_path_request_path_forwards_endpoints():
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"found": False}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_path("jeevy", "n1", "n2", max_hops=3, scope="shop:10"))
    path = captured["path"]
    assert "from=n1" in path
    assert "to=n2" in path
    assert "max_hops=3" in path
    assert "scope=shop%3A10" in path
