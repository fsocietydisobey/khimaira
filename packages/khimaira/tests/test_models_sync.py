"""Tests for `khimaira models sync` (#57).

The user registry at ~/.khimaira/models.yaml needs periodic
reconciliation against the shipped defaults. This test suite
exercises the diff math + the apply path + the user-only
preservation guarantee.

Tests verify:
  - empty user registry → shipped defaults all show as "added"
  - matching registries → no changes
  - user has extra model → preserved across sync --apply
  - shipped pricing changed → flagged in diff
  - --apply writes the merged registry atomically with backup
  - malformed user YAML → refuses to sync (exit 3)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root ~/.khimaira/models.yaml at a tmp path via XDG_CONFIG_HOME."""
    config_root = tmp_path / "config"
    config_root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    from khimaira.dispatch import registry as registry_mod
    from khimaira.cli import models as models_cmd
    importlib.reload(registry_mod)
    importlib.reload(models_cmd)
    yield config_root / "khimaira" / "models.yaml"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    importlib.reload(registry_mod)
    importlib.reload(models_cmd)


def _write_user_yaml(path: Path, models: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"models": models}, sort_keys=False))


def test_load_default_models_returns_shipped_set(isolated_config):
    """Shipped defaults parse without error and include known Claude entries."""
    from khimaira.cli import models as models_cmd

    shipped = models_cmd._load_default_models()
    assert "claude-haiku-4-5" in shipped
    assert "claude-opus-4-7" in shipped
    assert shipped["claude-haiku-4-5"].runner == "claude"


def test_load_user_models_missing_file_returns_empty(isolated_config):
    """No user file → empty dict, no error."""
    from khimaira.cli import models as models_cmd

    assert models_cmd._load_user_models(isolated_config) == {}


def test_diff_empty_user_shows_all_as_added(isolated_config):
    """User has nothing → diff shows every shipped model as 'added_in_default'."""
    from khimaira.cli import models as models_cmd

    shipped = models_cmd._load_default_models()
    user = {}
    diff = models_cmd._diff_registries(shipped, user)

    assert len(diff.added_in_default) == len(shipped)
    assert diff.removed_in_default == []
    assert diff.changed == []
    assert diff.user_only == []


def test_diff_matching_registries_has_no_changes(isolated_config):
    """User has exactly the shipped defaults → diff is empty."""
    from khimaira.cli import models as models_cmd

    shipped = models_cmd._load_default_models()
    user = dict(shipped)  # shallow copy — same entries
    diff = models_cmd._diff_registries(shipped, user)

    assert not diff.has_changes()
    assert diff.user_only == []


def test_diff_detects_user_only_entries(isolated_config):
    """User has an extra model not in shipped → flagged as user_only."""
    from khimaira.cli import models as models_cmd
    from khimaira.dispatch.registry import ModelCost, ModelEntry

    shipped = models_cmd._load_default_models()
    user = dict(shipped)
    user["my-custom-model"] = ModelEntry(
        id="my-custom-model",
        runner="llm",
        capabilities=("code",),
        cost_per_1m=ModelCost(input=1.0, output=2.0),
    )
    diff = models_cmd._diff_registries(shipped, user)

    assert len(diff.user_only) == 1
    assert diff.user_only[0].id == "my-custom-model"
    assert not diff.has_changes()  # user_only doesn't count as a "change"


def test_diff_detects_pricing_change(isolated_config):
    """User pinned an old price; shipped has a new price → changed entry."""
    from khimaira.cli import models as models_cmd
    from khimaira.dispatch.registry import ModelCost, ModelEntry

    shipped = models_cmd._load_default_models()
    # Copy claude-haiku-4-5 from shipped, then mutate the price
    base = shipped["claude-haiku-4-5"]
    user = dict(shipped)
    user["claude-haiku-4-5"] = ModelEntry(
        id=base.id,
        runner=base.runner,
        capabilities=base.capabilities,
        cost_per_1m=ModelCost(input=999.0, output=999.0),  # absurd, will diff
        subscription=base.subscription,
        enabled_for_auto=base.enabled_for_auto,
    )
    diff = models_cmd._diff_registries(shipped, user)

    assert len(diff.changed) == 1
    user_entry, shipped_entry = diff.changed[0]
    assert user_entry.cost_per_1m.input == 999.0
    assert shipped_entry.cost_per_1m.input != 999.0


def test_sync_apply_writes_merged_registry(isolated_config):
    """--apply writes the shipped set + preserves user-only entries."""
    from khimaira.cli import models as models_cmd

    # User starts with an extra model
    _write_user_yaml(
        isolated_config,
        [
            {
                "id": "my-only-model",
                "runner": "llm",
                "capabilities": ["code"],
                "cost_per_1m": {"input": 1.0, "output": 2.0},
            }
        ],
    )

    args = type("Args", (), {"apply": True, "yes": True})()
    rc = models_cmd._run_sync(args)
    assert rc == 0
    assert isolated_config.is_file()

    # Reload and verify
    written = yaml.safe_load(isolated_config.read_text())
    ids = [m["id"] for m in written["models"]]
    assert "my-only-model" in ids  # user-only preserved
    assert "claude-haiku-4-5" in ids  # shipped default landed


def test_sync_apply_creates_backup(isolated_config):
    """When applying over an existing file, a .yaml.bak.<mtime> backup is created."""
    from khimaira.cli import models as models_cmd

    _write_user_yaml(
        isolated_config,
        [{"id": "claude-haiku-4-5", "runner": "claude"}],  # minimal — will diff
    )

    args = type("Args", (), {"apply": True, "yes": True})()
    rc = models_cmd._run_sync(args)
    assert rc == 0

    # Find the backup
    backups = list(isolated_config.parent.glob("models.yaml.bak.*"))
    assert len(backups) == 1


def test_sync_read_only_does_not_write(isolated_config):
    """Without --apply, sync only prints the diff."""
    from khimaira.cli import models as models_cmd

    assert not isolated_config.is_file()
    args = type("Args", (), {"apply": False, "yes": False})()
    rc = models_cmd._run_sync(args)
    assert rc == 0
    # No file was created
    assert not isolated_config.is_file()


def test_sync_malformed_user_yaml_refuses(isolated_config, capsys):
    """Broken YAML → exit 3, don't risk clobbering."""
    from khimaira.cli import models as models_cmd

    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("not: valid: yaml: [unclosed")

    args = type("Args", (), {"apply": True, "yes": True})()
    with pytest.raises(SystemExit) as exc:
        models_cmd._run_sync(args)
    assert exc.value.code == 3


def test_serialize_round_trip(isolated_config):
    """Output of _serialize_registry parses back via _parse_entry."""
    from khimaira.cli import models as models_cmd

    shipped = models_cmd._load_default_models()
    yaml_text = models_cmd._serialize_registry(list(shipped.values()))
    parsed = yaml.safe_load(yaml_text)
    assert "models" in parsed
    assert len(parsed["models"]) == len(shipped)
    # Each entry round-trips back to ModelEntry without error
    for raw in parsed["models"]:
        models_cmd._parse_entry(raw)
