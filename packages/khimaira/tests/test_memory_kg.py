"""Tests for the memory knowledge-graph layer (monitor/memory_kg.py + its
/internal/memory-kg router + the registry virtual-adapter helper).

Everything runs against tmp_path fixture files and a tmp_path SQLite store —
the real MEMORY.md / MEMORY_ARCHIVE.md files are never read or written (the
conftest autouse fixture additionally redirects configured paths defensively).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from khimaira import claude_memory_retrieval as memory
from khimaira.monitor import memory_kg
from khimaira.monitor.api import graph as graph_api


def _write_corpus(tmp_path: Path) -> list[memory.MemorySource]:
    """Two-project fixture corpus with typed topic files + an archive."""
    kh = tmp_path / "khimaira-mem"
    jv = tmp_path / "jeevy-mem"
    kh.mkdir()
    jv.mkdir()

    (kh / "MEMORY.md").write_text(
        "# index\n"
        "- [Alpha](feedback_alpha.md) — frontmatter-typed entry\n"
        "- [Beta](project_beta.md) — prefix-typed entry (no topic file)\n"
        "- [Gamma](gamma.md) — untyped entry\n",
        encoding="utf-8",
    )
    (kh / "feedback_alpha.md").write_text(
        "---\nname: alpha\ndescription: d\nmetadata:\n  type: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    (kh / "MEMORY_ARCHIVE.md").write_text(
        "- [Old ref](reference_old.md) — archived entry\n", encoding="utf-8"
    )

    (jv / "MEMORY.md").write_text(
        "- [Jeevy note](project_note.md) — jeevy entry\n", encoding="utf-8"
    )

    return [
        memory.MemorySource(
            project="khimaira",
            index_path=kh / "MEMORY.md",
            archive_path=kh / "MEMORY_ARCHIVE.md",
        ),
        memory.MemorySource(
            project="jeevy",
            index_path=jv / "MEMORY.md",
            archive_path=jv / "MEMORY_ARCHIVE.md",
        ),
    ]


@pytest.fixture
def kg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[memory.MemorySource]:
    """Fixture corpus wired in as the configured sources + a tmp SQLite store."""
    sources = _write_corpus(tmp_path)
    monkeypatch.setenv("KHIMAIRA_MEMORY_KG_DB", str(tmp_path / "memory_kg.sqlite3"))
    monkeypatch.setenv("KHIMAIRA_MEMORY_KHIMAIRA_INDEX", str(sources[0].index_path))
    monkeypatch.setenv("KHIMAIRA_MEMORY_JEEVY_INDEX", str(sources[1].index_path))
    return sources


def _node_by_label(payload: dict, label: str) -> dict:
    matches = [n for n in payload["data"]["nodes"] if n["label"] == label]
    assert len(matches) == 1, f"expected exactly one node labeled {label!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Node derivation
# ---------------------------------------------------------------------------


def test_nodes_derived_from_memory_files(kg_env):
    payload = memory_kg.graph_payload()
    nodes = payload["data"]["nodes"]
    assert len(nodes) == 5

    alpha = _node_by_label(payload, "Alpha")
    assert alpha["type"] == "feedback"  # from topic-file frontmatter
    assert alpha["id"] == memory._point_id("khimaira", "feedback_alpha.md")
    assert "badge" not in alpha

    assert _node_by_label(payload, "Beta")["type"] == "project"  # filename prefix
    assert _node_by_label(payload, "Gamma")["type"] == "memory"  # fallback

    old = _node_by_label(payload, "Old ref")
    assert old["badge"] == "archived"
    # Same uuid5 identity the Qdrant layer uses.
    assert old["id"] == memory._point_id("khimaira", "reference_old.md")


def test_scope_filters_to_one_project(kg_env):
    payload = memory_kg.graph_payload(scope="jeevy")
    labels = [n["label"] for n in payload["data"]["nodes"]]
    assert labels == ["Jeevy note"]


# ---------------------------------------------------------------------------
# Edge store
# ---------------------------------------------------------------------------


def test_edge_add_list_round_trip_and_idempotence(kg_env):
    first = memory_kg.link_entries(
        "khimaira", "feedback_alpha.md", "project_beta.md", "RELATES_TO", note="why"
    )
    assert first["created"] is True

    again = memory_kg.link_entries("khimaira", "feedback_alpha.md", "project_beta.md", "RELATES_TO")
    assert again["created"] is False
    assert again["id"] == first["id"]

    edges = memory_kg.list_edges()
    assert len(edges) == 1
    assert edges[0]["note"] == "why"
    assert edges[0]["from_id"] == memory._point_id("khimaira", "feedback_alpha.md")

    graph_edges = memory_kg.graph_payload()["data"]["edges"]
    assert graph_edges == [
        {
            "id": first["id"],
            "from": memory._point_id("khimaira", "feedback_alpha.md"),
            "to": memory._point_id("khimaira", "project_beta.md"),
            "type": "RELATES_TO",
        }
    ]


def test_unknown_edge_type_rejected(kg_env):
    with pytest.raises(ValueError, match="unknown edge type"):
        memory_kg.link_entries("khimaira", "feedback_alpha.md", "project_beta.md", "MERGED_WITH")
    assert memory_kg.list_edges() == []


def test_self_loop_rejected(kg_env):
    with pytest.raises(ValueError, match="self-loop"):
        memory_kg.link_entries("khimaira", "feedback_alpha.md", "feedback_alpha.md", "RELATES_TO")


def test_unknown_link_rejected(kg_env):
    with pytest.raises(ValueError, match="ghost.md"):
        memory_kg.link_entries("khimaira", "feedback_alpha.md", "ghost.md", "RELATES_TO")
    # Archived entries are linkable; cross-corpus links from the wrong project are not.
    memory_kg.link_entries("khimaira", "feedback_alpha.md", "reference_old.md", "SUPERSEDES")
    with pytest.raises(ValueError, match="unknown project"):
        memory_kg.link_entries("nope", "a.md", "b.md", "RELATES_TO")


# ---------------------------------------------------------------------------
# Health / dangling edges
# ---------------------------------------------------------------------------


def test_dangling_edge_counted_in_health_but_kept_in_graph(kg_env):
    sources = kg_env
    memory_kg.link_entries("khimaira", "feedback_alpha.md", "gamma.md", "CAUSED_BY")

    # Entry disappears from the index afterwards → the edge dangles.
    sources[0].index_path.write_text(
        "- [Alpha](feedback_alpha.md) — frontmatter-typed entry\n", encoding="utf-8"
    )

    health = memory_kg.health_payload()["data"]
    assert health["dangling_edges"] == 1
    assert health["edges"] == 1
    assert health["archived_nodes"] == 1

    # Reported as-is in the graph payload (the viewer tolerates it).
    assert len(memory_kg.graph_payload()["data"]["edges"]) == 1


def test_health_counts(kg_env):
    memory_kg.link_entries("khimaira", "feedback_alpha.md", "project_beta.md", "RELATES_TO")
    health = memory_kg.health_payload()["data"]
    assert health["nodes"] == 5
    assert health["edges"] == 1
    assert health["dangling_edges"] == 0
    assert health["nodes_by_type"] == {
        "feedback": 1,
        "project": 2,
        "memory": 1,
        "reference": 1,
    }
    assert health["edges_by_type"] == {"RELATES_TO": 1}


# ---------------------------------------------------------------------------
# Contract conformance (graph.py's own field rules — the gate must not drop us)
# ---------------------------------------------------------------------------


def test_graph_payload_conforms_to_contract(kg_env):
    memory_kg.link_entries("khimaira", "feedback_alpha.md", "reference_old.md", "SUPERSEDES")
    payload = memory_kg.graph_payload()
    for node in payload["data"]["nodes"]:
        assert graph_api._node_violations(node) == []
    for edge in payload["data"]["edges"]:
        assert graph_api._edge_violations(edge) == []
    # The daemon-side gate passes the payload through untouched.
    assert graph_api._filter_to_contract(payload, strict=True) == payload


def test_schema_payload_conforms_to_contract(kg_env):
    memory_kg.link_entries("khimaira", "feedback_alpha.md", "project_beta.md", "RELATES_TO")
    payload = memory_kg.schema_payload()
    for node in payload["data"]["nodes"]:
        assert graph_api._node_violations(node) == []
    for edge in payload["data"]["edges"]:
        assert graph_api._edge_violations(edge) == []
    type_nodes = {n["label"]: n["badge"] for n in payload["data"]["nodes"]}
    assert type_nodes["feedback"] == 1
    assert {"from": "type:feedback", "to": "type:project", "type": "RELATES_TO"} in payload["data"][
        "edges"
    ]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.fixture
def kg_client(kg_env) -> TestClient:
    from khimaira.monitor.api import memory_kg as memory_kg_api

    app = FastAPI()
    app.include_router(memory_kg_api.build_router(), prefix="/internal/memory-kg")
    return TestClient(app)


def test_router_graph_and_health(kg_client):
    r = kg_client.get("/internal/memory-kg/graph")
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]["nodes"]) == 5
    assert body["data"]["edges"] == []

    r = kg_client.get("/internal/memory-kg/graph", params={"scope": "jeevy"})
    assert [n["label"] for n in r.json()["data"]["nodes"]] == ["Jeevy note"]

    assert kg_client.get("/internal/memory-kg/health").json()["data"]["nodes"] == 5


def test_router_node_found_and_missing(kg_client):
    node_id = memory._point_id("khimaira", "feedback_alpha.md")
    r = kg_client.get(f"/internal/memory-kg/node/{node_id}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["found"] is True
    assert data["label"] == "Alpha"
    assert data["project"] == "khimaira"
    assert data["edges"] == []

    r = kg_client.get("/internal/memory-kg/node/not-a-real-id")
    assert r.status_code == 200  # graceful-empty, not an error
    assert r.json()["data"]["found"] is False


def test_router_schema(kg_client):
    r = kg_client.get("/internal/memory-kg/schema")
    assert r.status_code == 200
    assert {n["label"] for n in r.json()["data"]["nodes"]} >= {"feedback", "project"}


# ---------------------------------------------------------------------------
# Registry virtual-adapter helper
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.attach import registry as reg

    importlib.reload(reg)
    yield reg
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(reg)


def test_set_virtual_kg_adapter_creates_and_updates(isolated_registry):
    reg = isolated_registry
    reg.set_virtual_kg_adapter(
        "khimaira-memory", url="http://127.0.0.1:8740/internal/memory-kg/graph"
    )
    assert reg.get_kg_adapter("khimaira-memory") == {
        "url": "http://127.0.0.1:8740/internal/memory-kg/graph"
    }
    entry = next(e for e in reg.list_attached() if e.get("label") == "khimaira-memory")
    assert entry["virtual"] is True

    # Idempotent re-registration (e.g. a different port) updates, not duplicates.
    reg.set_virtual_kg_adapter(
        "khimaira-memory", url="http://127.0.0.1:9999/internal/memory-kg/graph"
    )
    labels = [e.get("label") for e in reg.list_attached()]
    assert labels.count("khimaira-memory") == 1
    assert reg.get_kg_adapter("khimaira-memory")["url"].startswith("http://127.0.0.1:9999")


def test_virtual_entry_skipped_by_attach_supervisor(isolated_registry, monkeypatch):
    reg = isolated_registry
    reg.set_virtual_kg_adapter("khimaira-memory", url="http://x/graph")

    import asyncio

    from khimaira.monitor import attach_supervisor

    monkeypatch.setattr(attach_supervisor, "list_attached", reg.list_attached)

    def _boom(*_a, **_k):  # would only fire if the virtual entry weren't skipped
        raise AssertionError("attach_project must not be called for a virtual entry")

    monkeypatch.setattr(attach_supervisor, "attach_project", _boom)
    asyncio.run(attach_supervisor.startup_reattach_pass())


def test_set_virtual_kg_adapter_annotates_real_entry_when_label_exists(
    isolated_registry, tmp_path
):
    """The production path since the label moved to "khimaira": when a REAL
    attached entry matches the label, the adapter annotates it in place — no
    virtual placeholder is created alongside it."""
    reg = isolated_registry
    reg.record_attach(tmp_path / "khimaira", tmp_path / "khimaira" / ".venv", label="khimaira")

    reg.set_virtual_kg_adapter("khimaira", url="http://127.0.0.1:8740/internal/memory-kg/graph")

    entries = [e for e in reg.list_attached() if e.get("label") == "khimaira"]
    assert len(entries) == 1
    assert entries[0].get("virtual") is not True
    assert reg.get_kg_adapter("khimaira")["url"].endswith("/internal/memory-kg/graph")
