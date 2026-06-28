"""#38 Tier-2 — KG generic-contract gate (the boundary tollgate).

The proxy (`api/graph.py`) returns a per-project KG adapter's response
**verbatim** — it deliberately never inspects the shape (it stays code-agnostic).
`test_graph_api.py` covers the *transport* (auth header, 404/500/502, passthrough)
but NOTHING asserts that what an adapter returns actually CONFORMS to the
khimaira-OWNED generic contract. That conformance is the contract-gate: the place
where "the adapter must speak our schema" is enforced (ai-engineering tollgate —
validate at the boundary; the renderer downstream assumes the shape).

WHY this is "gate against the source of truth, not a hand-copied shape": the
validator's field rules are PARSED from the schema source of truth
(`apps/monitor-ui/src/components/kg/kgTypes.ts`) at test time, not re-typed here. A
drift-pin (`test_kgtypes_source_matches_pinned_contract`) asserts the parsed shape
equals an explicit expectation, so any edit to the TS interfaces fails loud and
forces a conscious update of the gate — the gate can't silently drift from the
contract it guards.

The contract (kgTypes.ts §GraphNode/§GraphEdge/§GraphResponse, SPEC kg-graph-mapper
§137), wire shape served at `GET /api/graph/<project>`:

    { data: { nodes: GraphNode[], edges: GraphEdge[] } }
    GraphNode = { id: string, type: string, label: string, badge?: string|number }
    GraphEdge = { id?: string, from: string, to: string, type: string, weight?: number }

`type` is an OPAQUE string (any value allowed); the gate enforces FIELD NAMES +
types + required/optional + NO extra fields — an extra field is how a jeevy schema
term (`node_type`, `canonical_key`, `display_name`) would leak past the adapter and
break the "zero jeevy terms in the viewer" invariant.

NOTE: spec/test only — no daemon/route change, nothing deployed. This adds the gate
as an assertion; wiring it INTO the proxy as a live 502-on-nonconforming tollgate is
a separate, deploy-gated follow-up (see the module-end note).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the schema source of truth (walk up to the repo root that holds it).
# A MISSING source is a FAILURE, never a skip — the gate guards that file; if it
# vanished, the contract's source of truth is gone (escaped-bugs meta-gate: a skip
# is not a pass).
# ---------------------------------------------------------------------------

_KG_TYPES_REL = Path("apps/monitor-ui/src/components/kg/kgTypes.ts")
_SPEC_REL = Path("tasks/kg-graph-mapper/SPEC.md")


def _find_repo_file(rel: Path) -> Path:
    for base in Path(__file__).resolve().parents:
        candidate = base / rel
        if candidate.exists():
            return candidate
    raise AssertionError(
        f"contract source of truth not found: {rel} — searched up from "
        f"{Path(__file__).resolve()}. The gate cannot validate against a missing "
        f"schema; this is a failure, not a skip."
    )


# ---------------------------------------------------------------------------
# Minimal TS-interface parser (the file is Prettier-formatted; one field/line).
# Extracts {field -> {optional, ts_type}} for an `export interface <Name> { ... }`.
# ---------------------------------------------------------------------------

# `  field?: string | number;`  (comments and blank lines skipped)
_FIELD_RE = re.compile(r"^\s*(?P<name>\w+)(?P<opt>\??)\s*:\s*(?P<type>[^;]+);")


def _parse_ts_interface(src: str, name: str) -> dict[str, dict]:
    m = re.search(rf"export interface {re.escape(name)}\s*\{{", src)
    assert m, f"interface {name} not found in kgTypes.ts"
    # Walk from the opening brace to its matching close (no nested braces in these
    # flat interfaces, so the first `}` at line start closes it).
    body = src[m.end() :]
    end = body.index("\n}")
    fields: dict[str, dict] = {}
    for line in body[:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("/*", "*", "//")):
            continue
        fm = _FIELD_RE.match(line)
        if not fm:
            continue
        fields[fm["name"]] = {
            "optional": fm["opt"] == "?",
            "ts_type": fm["type"].strip(),
        }
    return fields


def _ts_type_checker(ts_type: str):
    """Return a predicate(value) for a primitive TS type / union used in the
    contract. Handles `string`, `number`, and `string | number`. bool is NOT a
    valid number (Python bool is an int subclass — exclude it explicitly)."""
    parts = {p.strip() for p in ts_type.split("|")}

    def check(value) -> bool:
        for p in parts:
            if p == "string" and isinstance(value, str):
                return True
            if p == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
        return False

    return check


# ---------------------------------------------------------------------------
# The gate: validate a wire payload against a parsed schema. Returns a list of
# human-readable violations (empty == conforms). Pure + deterministic.
# ---------------------------------------------------------------------------


def _validate_item(item, schema: dict[str, dict], where: str) -> list[str]:
    if not isinstance(item, dict):
        return [f"{where} is not an object"]
    errs: list[str] = []
    for field, spec in schema.items():
        if field not in item:
            if not spec["optional"]:
                errs.append(f"{where} missing required field '{field}'")
            continue
        if not _ts_type_checker(spec["ts_type"])(item[field]):
            errs.append(f"{where}.{field}={item[field]!r} violates type {spec['ts_type']!r}")
    extra = sorted(set(item) - set(schema))
    if extra:
        # Extra field == a non-contract term leaking through the adapter (e.g. a raw
        # jeevy column). This is the core "zero jeevy terms" invariant.
        errs.append(f"{where} has non-contract field(s) {extra} (schema-term leak)")
    return errs


def validate_graph_response(
    payload, node_schema: dict[str, dict], edge_schema: dict[str, dict]
) -> list[str]:
    """Validate the `GET /api/graph/<project>` wire shape `{data:{nodes,edges}}`."""
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    if "data" not in payload:
        return ["missing top-level 'data' wrapper (GraphResponse = {data:{...}})"]
    data = payload["data"]
    if not isinstance(data, dict):
        return ["'data' is not an object"]
    errs: list[str] = []
    for coll in ("nodes", "edges"):
        if coll not in data:
            errs.append(f"missing data.{coll}")
        elif not isinstance(data[coll], list):
            errs.append(f"data.{coll} is not a list")
    if errs:
        return errs
    for i, node in enumerate(data["nodes"]):
        errs += _validate_item(node, node_schema, f"nodes[{i}]")
    for i, edge in enumerate(data["edges"]):
        errs += _validate_item(edge, edge_schema, f"edges[{i}]")
    return errs


# ---------------------------------------------------------------------------
# Fixtures: the parsed schemas + the explicit drift-pin.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kg_src() -> str:
    return _find_repo_file(_KG_TYPES_REL).read_text()


@pytest.fixture(scope="module")
def node_schema(kg_src) -> dict[str, dict]:
    return _parse_ts_interface(kg_src, "GraphNode")


@pytest.fixture(scope="module")
def edge_schema(kg_src) -> dict[str, dict]:
    return _parse_ts_interface(kg_src, "GraphEdge")


# field -> optional?  (the pinned contract; changing kgTypes.ts must trip this)
_PIN_NODE = {"id": False, "type": False, "label": False, "badge": True}
_PIN_EDGE = {"id": True, "from": False, "to": False, "type": False, "weight": True}


# ---------------------------------------------------------------------------
# Drift-pin — the source-of-truth lock.
# ---------------------------------------------------------------------------


def test_kgtypes_source_matches_pinned_contract(node_schema, edge_schema):
    """The gate is parsed from kgTypes.ts, but its EXPECTED shape is pinned here so
    an edit to the TS interfaces fails loud and forces the gate (and any adapter)
    to be re-reviewed — the gate can't silently track a contract change."""
    assert {f: s["optional"] for f, s in node_schema.items()} == _PIN_NODE, (
        "GraphNode shape in kgTypes.ts drifted from the pinned contract"
    )
    assert {f: s["optional"] for f, s in edge_schema.items()} == _PIN_EDGE, (
        "GraphEdge shape in kgTypes.ts drifted from the pinned contract"
    )
    # Primitive types are what the gate enforces — pin them too.
    assert node_schema["badge"]["ts_type"] == "string | number"
    assert edge_schema["weight"]["ts_type"] == "number"


def test_spec_documents_generic_contract():
    """SPEC §137 is the prose source of truth co-cited with kgTypes.ts — assert it
    still documents the generic `{nodes, edges}` contract, so a SPEC rewrite that
    drops the architecture is flagged."""
    spec = _find_repo_file(_SPEC_REL).read_text()
    assert "code-agnostic" in spec
    assert "{nodes:[{id, type, label, badge?}], edges:[{from, to, type, weight?}]}" in spec


# ---------------------------------------------------------------------------
# Positive conformance — a generic-shaped adapter response passes.
# ---------------------------------------------------------------------------


def _ok_payload() -> dict:
    return {
        "data": {
            "nodes": [
                {"id": "n1", "type": "shop", "label": "Shop 10"},  # no badge (optional)
                {"id": "n2", "type": "job", "label": "JOB-1", "badge": 5},  # number badge
                {"id": "n3", "type": "part", "label": "Plate", "badge": "hot"},  # string badge
            ],
            "edges": [
                {"from": "n2", "to": "n1", "type": "owns"},  # no id/weight (optional)
                {"id": "e1", "from": "n3", "to": "n2", "type": "for-part", "weight": 0.91},
            ],
        }
    }


def test_conforming_payload_passes(node_schema, edge_schema):
    assert validate_graph_response(_ok_payload(), node_schema, edge_schema) == []


def test_empty_graph_conforms(node_schema, edge_schema):
    """An empty-but-well-formed graph (a shop with no captured KG yet) conforms."""
    payload = {"data": {"nodes": [], "edges": []}}
    assert validate_graph_response(payload, node_schema, edge_schema) == []


# ---------------------------------------------------------------------------
# Negative conformance — the unhappy paths the gate must reject.
# ---------------------------------------------------------------------------


def test_missing_data_wrapper_rejected(node_schema, edge_schema):
    payload = {"nodes": [], "edges": []}  # no `data`
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("data" in e for e in errs)


def test_node_missing_required_field_rejected(node_schema, edge_schema):
    payload = {"data": {"nodes": [{"id": "n1", "type": "shop"}], "edges": []}}  # no label
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("missing required field 'label'" in e for e in errs)


def test_edge_missing_required_field_rejected(node_schema, edge_schema):
    payload = {"data": {"nodes": [], "edges": [{"from": "a", "type": "owns"}]}}  # no `to`
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("missing required field 'to'" in e for e in errs)


def test_jeevy_term_leak_rejected(node_schema, edge_schema):
    """The core invariant: a raw jeevy column leaking through the adapter (here
    `node_type`/`canonical_key` instead of the opaque `type`) is a non-contract
    field and must fail the gate."""
    payload = {
        "data": {
            "nodes": [{"id": "n1", "node_type": "shop", "canonical_key": "shop:10", "label": "S"}],
            "edges": [],
        }
    }
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("schema-term leak" in e for e in errs)
    assert any("missing required field 'type'" in e for e in errs)


def test_wrong_type_rejected(node_schema, edge_schema):
    payload = {
        "data": {
            "nodes": [{"id": "n1", "type": "shop", "label": "S"}],
            "edges": [{"from": "n1", "to": "n1", "type": "self", "weight": "high"}],  # str
        }
    }
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("weight" in e and "number" in e for e in errs)


def test_bool_is_not_a_valid_number_badge(node_schema, edge_schema):
    """Python bool is an int subclass; the contract's `string | number` must NOT
    accept a bool badge (would render nonsensically)."""
    payload = {
        "data": {"nodes": [{"id": "n1", "type": "s", "label": "S", "badge": True}], "edges": []}
    }
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("badge" in e for e in errs)


def test_collections_must_be_lists(node_schema, edge_schema):
    payload = {"data": {"nodes": {}, "edges": []}}
    errs = validate_graph_response(payload, node_schema, edge_schema)
    assert any("data.nodes is not a list" in e for e in errs)


# Module-end note (for the follow-up that wires this LIVE):
# To make this a runtime tollgate, `api/graph.get_graph` would call
# `validate_graph_response(...)` on the adapter payload and raise HTTPException(502,
# "adapter response violates KG contract: ...") on a non-empty violation list —
# turning a silent shape-mismatch into a loud, debuggable boundary failure. That is a
# daemon change (restart) and is HELD per the §5 deploy gate; this file is the
# spec/gate it would enforce.
