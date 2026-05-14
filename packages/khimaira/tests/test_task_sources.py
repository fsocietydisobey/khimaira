"""Tests for `khimaira.task_sources` — Phase 1.5 generic implementation.

Covers:
  - `Task` dataclass + `TaskSource` Protocol shape
  - `JsonlTaskSource`: happy path, missing file, malformed lines, closed
    states excluded, no `KHIMAIRA_TASKS_JSONL` collision
  - `fetch_all_open_tasks`: fan-out, hook_safe filter, exception
    isolation between adapters
  - `load_configured_sources`: defaults, custom config, malformed YAML,
    unknown kind logged-not-raised
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root HOME + XDG_CONFIG_HOME so we don't touch real ~/.khimaira."""
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("KHIMAIRA_TASKS_JSONL", raising=False)
    # Reload modules so their os.path.expanduser captures NEW HOME
    from khimaira.task_sources import jsonl as jsonl_mod
    importlib.reload(jsonl_mod)
    from khimaira.task_sources import config as config_mod
    importlib.reload(config_mod)
    yield home, config
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    importlib.reload(jsonl_mod)
    importlib.reload(config_mod)


# -------------------- Task / Protocol -------------------- #


def test_task_dataclass_defaults():
    from khimaira.task_sources import Task

    t = Task(id="X-1", title="hello")
    assert t.id == "X-1"
    assert t.title == "hello"
    assert t.state == ""
    assert t.source == ""
    assert t.tags == []


# -------------------- JsonlTaskSource -------------------- #


async def test_jsonl_missing_file_returns_empty(isolated_home):
    home, _ = isolated_home
    from khimaira.task_sources.jsonl import JsonlTaskSource

    src = JsonlTaskSource(path=home / "no-such.jsonl")
    assert src.hook_safe() is True
    assert await src.fetch_open_tasks() == []


async def test_jsonl_reads_open_tasks(isolated_home):
    home, _ = isolated_home
    path = home / "todo.jsonl"
    path.write_text(
        '{"id": "T-1", "title": "first", "state": "todo"}\n'
        '{"id": "T-2", "title": "second", "state": "in progress"}\n'
    )
    from khimaira.task_sources.jsonl import JsonlTaskSource

    src = JsonlTaskSource(path=path)
    tasks = await src.fetch_open_tasks()
    assert len(tasks) == 2
    assert tasks[0].id == "T-1"
    assert tasks[0].source == "jsonl"
    assert tasks[1].state == "in progress"


async def test_jsonl_excludes_closed_states(isolated_home):
    home, _ = isolated_home
    path = home / "todo.jsonl"
    path.write_text(
        '{"id": "T-1", "title": "open", "state": "todo"}\n'
        '{"id": "T-2", "title": "shipped", "state": "done"}\n'
        '{"id": "T-3", "title": "killed", "state": "cancelled"}\n'
        '{"id": "T-4", "title": "archived too", "state": "ARCHIVED"}\n'
    )
    from khimaira.task_sources.jsonl import JsonlTaskSource

    tasks = await JsonlTaskSource(path=path).fetch_open_tasks()
    assert [t.id for t in tasks] == ["T-1"]


async def test_jsonl_skips_malformed_lines(isolated_home):
    home, _ = isolated_home
    path = home / "todo.jsonl"
    path.write_text(
        '{"id": "T-1", "title": "first", "state": "todo"}\n'
        "not-json-{\n"
        "\n"
        "# comment line ignored\n"
        '{"id": "T-2", "title": "second"}\n'
    )
    from khimaira.task_sources.jsonl import JsonlTaskSource

    tasks = await JsonlTaskSource(path=path).fetch_open_tasks()
    assert [t.id for t in tasks] == ["T-1", "T-2"]


async def test_jsonl_env_var_path_override(monkeypatch, tmp_path):
    """KHIMAIRA_TASKS_JSONL env var should override the default path."""
    custom = tmp_path / "elsewhere.jsonl"
    custom.write_text('{"id": "X", "title": "via env"}\n')
    monkeypatch.setenv("KHIMAIRA_TASKS_JSONL", str(custom))
    # Reload to pick up env
    from khimaira.task_sources import jsonl as jsonl_mod
    importlib.reload(jsonl_mod)
    src = jsonl_mod.JsonlTaskSource()  # no explicit path → uses env default
    tasks = await src.fetch_open_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "X"


# -------------------- config + fan-out -------------------- #


async def test_load_configured_sources_default_when_no_config(isolated_home):
    home, _ = isolated_home
    from khimaira.task_sources.config import load_configured_sources

    sources = load_configured_sources()
    assert len(sources) == 1
    assert sources[0].name == "jsonl"


async def test_load_configured_sources_reads_yaml(isolated_home):
    home, config = isolated_home
    cfg = config / "khimaira" / "task_sources.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "sources:\n"
        "  - kind: jsonl\n"
        f"    path: {home / 'a.jsonl'}\n"
        "  - kind: jsonl\n"
        f"    path: {home / 'b.jsonl'}\n"
    )
    from khimaira.task_sources.config import load_configured_sources

    sources = load_configured_sources()
    assert len(sources) == 2


async def test_load_configured_sources_ignores_unknown_kind(isolated_home):
    home, config = isolated_home
    cfg = config / "khimaira" / "task_sources.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "sources:\n"
        "  - kind: not-a-real-tracker\n"  # not built-in; logs warning + skipped
        "    enabled: true\n"
        "  - kind: jsonl\n"
        f"    path: {home / 'todo.jsonl'}\n"
    )
    from khimaira.task_sources.config import load_configured_sources

    sources = load_configured_sources()
    assert len(sources) == 1
    assert sources[0].name == "jsonl"


async def test_load_configured_sources_disabled_filter(isolated_home):
    home, config = isolated_home
    cfg = config / "khimaira" / "task_sources.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "sources:\n"
        "  - kind: jsonl\n"
        f"    path: {home / 'a.jsonl'}\n"
        "    enabled: false\n"
        "  - kind: jsonl\n"
        f"    path: {home / 'b.jsonl'}\n"
        "    enabled: true\n"
    )
    from khimaira.task_sources.config import load_configured_sources

    sources = load_configured_sources()
    assert len(sources) == 1


async def test_fetch_all_open_tasks_merges_sources(isolated_home):
    home, _ = isolated_home
    a = home / "a.jsonl"
    a.write_text('{"id": "A-1", "title": "from A"}\n')
    b = home / "b.jsonl"
    b.write_text('{"id": "B-1", "title": "from B"}\n')

    from khimaira.task_sources.config import fetch_all_open_tasks
    from khimaira.task_sources.jsonl import JsonlTaskSource

    tasks = await fetch_all_open_tasks(
        [JsonlTaskSource(path=a), JsonlTaskSource(path=b)]
    )
    ids = {t.id for t in tasks}
    assert ids == {"A-1", "B-1"}


async def test_fetch_all_open_tasks_hook_safe_filter(isolated_home):
    """A non-hook-safe adapter is excluded when hook_safe_only=True."""
    home, _ = isolated_home
    from dataclasses import dataclass

    from khimaira.task_sources import Task
    from khimaira.task_sources.config import fetch_all_open_tasks
    from khimaira.task_sources.jsonl import JsonlTaskSource

    @dataclass
    class _MockMcpSource:
        name: str = "mcp-only"

        def hook_safe(self) -> bool:
            return False

        async def fetch_open_tasks(self):
            return [Task(id="MCP-1", title="from MCP")]

    a = home / "a.jsonl"
    a.write_text('{"id": "A-1", "title": "from JSONL"}\n')

    safe_only = await fetch_all_open_tasks(
        [JsonlTaskSource(path=a), _MockMcpSource()],
        hook_safe_only=True,
    )
    assert {t.id for t in safe_only} == {"A-1"}

    all_sources = await fetch_all_open_tasks(
        [JsonlTaskSource(path=a), _MockMcpSource()],
        hook_safe_only=False,
    )
    assert {t.id for t in all_sources} == {"A-1", "MCP-1"}


# -------------------- list_tasks MCP tool -------------------- #


async def test_list_tasks_mcp_renders_tasks(isolated_home, monkeypatch):
    """The `list_tasks` MCP tool returns a human-readable bullet list."""
    home, _ = isolated_home
    todo = home / ".khimaira" / "todo.jsonl"
    todo.parent.mkdir(parents=True)
    todo.write_text(
        '{"id":"local-1","title":"Wire up the thing","state":"in progress"}\n'
        '{"id":"local-2","title":"Document it","state":"todo"}\n'
    )

    # Force a JSONL-only configuration pointing at our fixture file so
    # we don't depend on whatever happens to be on the developer's box.
    from khimaira.task_sources.jsonl import JsonlTaskSource
    from khimaira.task_sources import config as cfg_mod

    monkeypatch.setattr(
        cfg_mod, "load_configured_sources", lambda: [JsonlTaskSource(path=todo)]
    )

    from khimaira.server import mcp as mcp_mod

    out = await mcp_mod.list_tasks()
    assert "📋 khimaira tasks — 2 open assignment(s):" in out
    assert "local-1" in out
    assert "Wire up the thing" in out
    # in-progress sorts above todo
    assert out.index("local-1") < out.index("local-2")


async def test_list_tasks_mcp_empty(isolated_home, monkeypatch):
    """`📭 no open tasks` when every configured source returns []."""
    from khimaira.task_sources import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_configured_sources", lambda: [])

    from khimaira.server import mcp as mcp_mod

    out = await mcp_mod.list_tasks()
    assert "📭 no open tasks" in out


async def test_list_tasks_mcp_hook_safe_only_filters_non_hook_safe(
    isolated_home, monkeypatch
):
    """`hook_safe_only=True` excludes adapters that report hook_safe()=False."""
    from dataclasses import dataclass

    from khimaira.task_sources import Task
    from khimaira.task_sources import config as cfg_mod

    @dataclass
    class _MockMcpSource:
        name: str = "mcp-only"

        def hook_safe(self) -> bool:
            return False

        async def fetch_open_tasks(self):
            return [Task(id="MCP-1", title="agent-only task", source="mcp-only")]

    monkeypatch.setattr(cfg_mod, "load_configured_sources", lambda: [_MockMcpSource()])

    from khimaira.server import mcp as mcp_mod

    # Default: include non-hook-safe → MCP-1 appears
    out_all = await mcp_mod.list_tasks()
    assert "MCP-1" in out_all

    # hook_safe_only=True: MCP-1 filtered out → empty
    out_safe = await mcp_mod.list_tasks(hook_safe_only=True)
    assert "📭 no open tasks" in out_safe
    assert "(hook-safe sources only)" in out_safe


async def test_fetch_all_open_tasks_exception_isolation(isolated_home):
    """One source raising doesn't kill the others."""
    home, _ = isolated_home
    from dataclasses import dataclass

    from khimaira.task_sources.config import fetch_all_open_tasks
    from khimaira.task_sources.jsonl import JsonlTaskSource

    @dataclass
    class _BrokenSource:
        name: str = "broken"

        def hook_safe(self) -> bool:
            return True

        async def fetch_open_tasks(self):
            raise RuntimeError("simulated source crash")

    a = home / "a.jsonl"
    a.write_text('{"id": "A-1", "title": "still here"}\n')

    tasks = await fetch_all_open_tasks(
        [_BrokenSource(), JsonlTaskSource(path=a)]
    )
    assert [t.id for t in tasks] == ["A-1"]


# -------------------- LinearTaskSource (skeleton, 2026-05-14) -------------------- #


async def test_linear_skeleton_returns_empty():
    """The skeleton adapter returns [] cleanly — no daemon endpoint yet.

    See tasks/linear-adapter/IMPLEMENTATION.md for the full impl plan.
    This test pins the contract: until daemon-side MCP dispatch lands,
    the adapter is a silent no-op, not a NotImplementedError + crash.
    """
    from khimaira.task_sources.linear import LinearTaskSource

    src = LinearTaskSource()
    assert src.name == "linear"
    assert src.hook_safe() is False  # flips to True after Step 3 in the spec
    assert await src.fetch_open_tasks() == []


def test_linear_kind_resolves_in_config_builder(isolated_home):
    """`{kind: linear, enabled: true}` in task_sources.yaml resolves to
    a LinearTaskSource — users can opt in today; the adapter silently
    returns nothing until the daemon endpoint ships."""
    from khimaira.task_sources.config import _build_source
    from khimaira.task_sources.linear import LinearTaskSource

    entry = {"kind": "linear", "enabled": True}
    source = _build_source(entry)
    assert source is not None
    assert isinstance(source, LinearTaskSource)
    assert source.name == "linear"


def test_linear_skeleton_carries_config_overrides():
    """Config overrides (daemon_port, timeout_s) reach the adapter — when
    the real impl lands, these tunables wire to the HTTP client."""
    from khimaira.task_sources.config import _build_source
    from khimaira.task_sources.linear import LinearTaskSource

    entry = {"kind": "linear", "daemon_port": 9999, "timeout_s": 5.0}
    source = _build_source(entry)
    assert isinstance(source, LinearTaskSource)
    assert source.daemon_port == 9999
    assert source.timeout_s == 5.0
