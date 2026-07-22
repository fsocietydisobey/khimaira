"""Tests for Claude native-memory prune, indexing, search, and scheduling."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from khimaira import claude_memory_retrieval as memory
from khimaira.cli import main as cli_main


class FakeQdrantClient:
    """Small behavioral fake for the Qdrant methods this module uses."""

    def __init__(self) -> None:
        self.exists = False
        self.points: dict[str, Any] = {}
        self.create_calls = 0
        self.query_filters: list[Any] = []

    def collection_exists(self, collection_name: str) -> bool:
        assert collection_name == "khimaira_memory"
        return self.exists

    def count(self, *, collection_name: str, exact: bool) -> SimpleNamespace:
        assert collection_name == "khimaira_memory"
        assert exact is True
        return SimpleNamespace(count=len(self.points))

    def delete_collection(self, collection_name: str) -> None:
        assert collection_name == "khimaira_memory"
        self.exists = False
        self.points.clear()

    def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        assert collection_name == "khimaira_memory"
        assert vectors_config.size == memory.EMBED_DIM
        self.exists = True
        self.create_calls += 1

    def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        assert collection_name == "khimaira_memory"
        for point in points:
            self.points[str(point.id)] = point

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        with_payload: bool,
    ) -> SimpleNamespace:
        assert collection_name == "khimaira_memory"
        assert query
        assert with_payload is True
        self.query_filters.append(query_filter)
        selected = list(self.points.values())
        for condition in query_filter.must if query_filter is not None else []:
            selected = [
                point
                for point in selected
                if point.payload.get(condition.key) == condition.match.value
            ]
        return SimpleNamespace(
            points=[
                SimpleNamespace(payload=point.payload, score=0.75) for point in selected[:limit]
            ]
        )


def _source(tmp_path: Path, project: str = "khimaira") -> memory.MemorySource:
    directory = tmp_path / project
    directory.mkdir(parents=True)
    return memory.MemorySource(
        project=project,
        index_path=directory / "MEMORY.md",
        archive_path=directory / "MEMORY_ARCHIVE.md",
        pins=("user_profile.md",) if project == "jeevy" else (),
    )


def _write_entries(source: memory.MemorySource, count: int, body_size: int = 20) -> None:
    lines = []
    for index in range(count):
        link = f"topic_{index}.md"
        lines.append(f"- [Topic {index}]({link}) — {'x' * body_size} {index}")
        topic = source.index_path.parent / link
        topic.write_text(f"# Topic {index}\n", encoding="utf-8")
        timestamp = time.time() - (count - index) * 10
        topic.touch()
        topic.chmod(0o600)
        os.utime(topic, (timestamp, timestamp))
    source.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def fake_qdrant(monkeypatch: pytest.MonkeyPatch) -> FakeQdrantClient:
    client = FakeQdrantClient()
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.setattr(memory, "_embed", lambda texts: [[0.1] * memory.EMBED_DIM for _ in texts])
    monkeypatch.setattr(memory, "_embed_query", lambda query: [0.2] * memory.EMBED_DIM)
    monkeypatch.setattr(memory, "_RAG_ENABLED", True)
    return client


def test_prune_then_reindex_round_trip(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
) -> None:
    source = _source(tmp_path)
    _write_entries(source, 4, body_size=300)

    result = memory.refresh_memories(sources=[source], max_bytes=500)

    assert result["projects"]["khimaira"]["status"] == "pruned"
    assert result["reindex"]["status"] == "rebuilt"
    assert result["reindex"]["indexed"] == 4
    payloads = [point.payload for point in fake_qdrant.points.values()]
    assert {payload["link"] for payload in payloads} == {
        "topic_0.md",
        "topic_1.md",
        "topic_2.md",
        "topic_3.md",
    }
    assert {payload["source_file"] for payload in payloads} == {
        "MEMORY.md",
        "MEMORY_ARCHIVE.md",
    }


def test_core_refresh_requires_explicit_sources() -> None:
    with pytest.raises(TypeError, match="sources"):
        memory.refresh_memories()  # type: ignore[call-arg]


def test_harness_redirects_configured_sources_and_disables_live_backends(
    tmp_path: Path,
) -> None:
    sources = memory.configured_sources()

    assert all(source.index_path.is_relative_to(tmp_path) for source in sources)
    assert memory.auto_refresh_enabled() is False
    assert memory._RAG_ENABLED is False


def test_fingerprint_skips_unchanged_rebuild(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    _write_entries(source, 2)
    first = memory.rebuild_memory_index(sources=[source])
    assert first["status"] == "rebuilt"

    monkeypatch.setattr(
        memory,
        "_embed",
        lambda texts: pytest.fail("unchanged content must not be embedded again"),
    )
    second = memory.rebuild_memory_index(sources=[source])

    assert second["status"] == "unchanged"
    assert fake_qdrant.create_calls == 1
    assert source.fingerprint_path.is_file()


def test_empty_collection_self_heals_even_with_matching_fingerprint(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
) -> None:
    source = _source(tmp_path)
    _write_entries(source, 1)
    memory.rebuild_memory_index(sources=[source])
    fake_qdrant.points.clear()

    result = memory.search_memory("topic", sources=[source])

    assert result["error"] is None
    assert len(result["hits"]) == 1
    assert fake_qdrant.create_calls == 2


def test_point_id_stays_stable_across_live_to_archive_move(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
) -> None:
    source = _source(tmp_path)
    _write_entries(source, 2, body_size=300)
    memory.rebuild_memory_index(sources=[source])
    point_id = memory._point_id("khimaira", "topic_0.md")
    assert fake_qdrant.points[point_id].payload["source_file"] == "MEMORY.md"

    memory.refresh_memories(sources=[source], max_bytes=400)

    assert point_id in fake_qdrant.points
    assert fake_qdrant.points[point_id].payload["source_file"] == "MEMORY_ARCHIVE.md"


def test_search_applies_project_and_archive_filters_server_side(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
) -> None:
    khimaira = _source(tmp_path, "khimaira")
    jeevy = _source(tmp_path, "jeevy")
    _write_entries(khimaira, 1)
    _write_entries(jeevy, 1)
    jeevy.archive_path.write_text("- [Old jeevy](old.md) — archived detail\n", encoding="utf-8")
    memory.rebuild_memory_index(sources=[khimaira, jeevy])

    result = memory.search_memory(
        "topic",
        project="jeevy",
        include_archived=False,
        sources=[khimaira, jeevy],
    )

    assert result["error"] is None
    assert len(result["hits"]) == 1
    assert result["hits"][0]["project"] == "jeevy"
    assert result["hits"][0]["source_file"] == "MEMORY.md"
    conditions = fake_qdrant.query_filters[-1].must
    assert {(condition.key, condition.match.value) for condition in conditions} == {
        ("project", "jeevy"),
        ("source_file", "MEMORY.md"),
    }


def test_search_qdrant_failure_is_a_clear_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory, "_RAG_ENABLED", True)

    def unavailable() -> Any:
        raise ConnectionError("qdrant refused connection")

    monkeypatch.setattr(memory, "_client", unavailable)

    result = memory.search_memory("old decision", sources=[])

    assert result["hits"] == []
    assert "qdrant refused connection" in result["error"]


async def test_periodic_refresh_requests_are_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_job(job_id: str) -> None:
        calls.append(job_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(memory, "_run_memory_refresh_job", slow_job)
    monkeypatch.setattr(memory, "_MEMORY_REFRESH_JOB_ID", None)
    monkeypatch.setattr(memory, "_MEMORY_REFRESH_TASK", None)
    monkeypatch.setattr(memory, "_MEMORY_REFRESH_STATE", None)
    monkeypatch.setenv("KHIMAIRA_MEMORY_AUTO_REFRESH", "1")

    first = memory.schedule_memory_refresh()
    await started.wait()
    second = memory.schedule_memory_refresh()

    assert second == first
    assert calls == [first]
    assert memory.get_memory_refresh_status()["in_progress"] is True
    release.set()
    assert memory._MEMORY_REFRESH_TASK is not None
    await memory._MEMORY_REFRESH_TASK


def test_periodic_refresh_is_disabled_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KHIMAIRA_MEMORY_AUTO_REFRESH", raising=False)
    monkeypatch.setattr(
        memory,
        "_run_memory_refresh_job",
        lambda job_id: pytest.fail("default-off scheduler created a live job"),
    )

    assert memory.schedule_memory_refresh() == ""


def test_parser_payload_includes_title_and_body(
    tmp_path: Path,
    fake_qdrant: FakeQdrantClient,
) -> None:
    source = _source(tmp_path)
    source.index_path.write_text(
        "- [Useful title](useful.md) — concise searchable body\n",
        encoding="utf-8",
    )

    memory.rebuild_memory_index(sources=[source])

    payload = next(iter(fake_qdrant.points.values())).payload
    assert payload == {
        "project": "khimaira",
        "source_file": "MEMORY.md",
        "title": "Useful title",
        "link": "useful.md",
        "body": "concise searchable body",
    }


def test_memory_refresh_cli_uses_combined_operation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"projects": {}, "reindex": {"status": "unchanged"}}

    monkeypatch.setattr(memory, "refresh_configured_memories", fake_refresh)

    assert cli_main(["memory", "refresh", "--max-bytes", "321", "--force-reindex"]) == 0
    assert calls == [{"max_bytes": 321, "force_reindex": True}]
    assert '"status": "unchanged"' in capsys.readouterr().out
