"""Tests for scarlet.generator.context_yaml (#66 write-side)."""

from __future__ import annotations

from pathlib import Path


def _make_project(tmp_path: Path, features: list[tuple[str, bool]]) -> Path:
    """Create a minimal project with feature dirs; bool = has CLAUDE.md.

    Uses src/features/ — one of scan_features' recognized fallback paths.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# Root CLAUDE.md", encoding="utf-8")
    feat_root = root / "src" / "features"
    feat_root.mkdir(parents=True)
    for name, has_doc in features:
        d = feat_root / name
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")
        if has_doc:
            (d / "CLAUDE.md").write_text(f"# {name}", encoding="utf-8")
    return root


def test_generate_returns_v1_schema_shape(tmp_path):
    """Generated dict has 'classes' + 'manual' with all 4 classes."""
    from scarlet.generator.context_yaml import CLASSES, generate_context_yaml

    root = _make_project(tmp_path, [("auth", True), ("dashboard", False)])
    data = generate_context_yaml(root)

    assert "classes" in data
    assert "manual" in data
    assert "generated_at" in data

    for cls in CLASSES:
        assert cls in data["classes"], f"Missing class {cls!r} in data['classes']"
        assert cls in data["manual"], f"Missing class {cls!r} in data['manual']"


def test_write_atomic_and_preserves_manual(tmp_path):
    """Write → set manual.architecture.pointers → regen → MANUAL preserved, classes updated. (DONE-GATE)"""
    from ruamel.yaml import YAML

    from scarlet.generator.context_yaml import _write_context_yaml, generate_context_yaml

    root = _make_project(tmp_path, [("auth", True)])
    target = tmp_path / ".khimaira" / "context.yaml"

    # Initial write
    data = generate_context_yaml(root)
    _write_context_yaml(target, data)
    assert target.is_file()

    # Manually set a human value in manual.architecture.pointers
    yaml = YAML()
    yaml.preserve_quotes = True
    existing = yaml.load(target.read_text(encoding="utf-8"))
    existing["manual"]["architecture"]["pointers"] = ["MY_HAND_WRITTEN_PTR.md"]
    buf = __import__("io").StringIO()
    yaml.dump(existing, buf)
    target.write_text(buf.getvalue(), encoding="utf-8")

    # Add a new feature WITH a CLAUDE.md so architecture pointers change → triggers regen
    new_feat = root / "src" / "features" / "newfeature"
    new_feat.mkdir()
    (new_feat / "__init__.py").write_text("", encoding="utf-8")
    (new_feat / "CLAUDE.md").write_text("# newfeature", encoding="utf-8")

    data2 = generate_context_yaml(root)
    _write_context_yaml(target, data2)

    # MANUAL section preserved
    final = yaml.load(target.read_text(encoding="utf-8"))
    assert "MY_HAND_WRITTEN_PTR.md" in final["manual"]["architecture"]["pointers"], (
        "MANUAL pointer must survive regen"
    )
    # AUTO architecture pointers updated (new feature with CLAUDE.md now appears)
    all_arch_ptrs = final["classes"]["architecture"]["pointers"]
    assert any("newfeature" in p for p in all_arch_ptrs), (
        "AUTO architecture pointers must include the new feature's CLAUDE.md"
    )


def test_scaffold_missing_docs_creates_file(tmp_path):
    """Feature without CLAUDE.md → _scaffold_missing_docs creates it (contains BEGIN MANUAL)."""
    from scarlet.generator.context_yaml import _scaffold_missing_docs

    root = _make_project(tmp_path, [("no_doc_feature", False)])
    feat_path = root / "src" / "features" / "no_doc_feature"
    assert not (feat_path / "CLAUDE.md").exists()

    result = _scaffold_missing_docs(root, feat_path)
    assert result is not None
    created = Path(result)
    assert created.is_file()
    content = created.read_text(encoding="utf-8")
    assert "BEGIN MANUAL" in content


def test_scaffold_missing_docs_no_overwrite(tmp_path):
    """Existing CLAUDE.md → _scaffold_missing_docs returns None, content unchanged."""
    from scarlet.generator.context_yaml import _scaffold_missing_docs

    root = _make_project(tmp_path, [("has_doc", True)])
    feat_path = root / "src" / "features" / "has_doc"
    original = (feat_path / "CLAUDE.md").read_text(encoding="utf-8")

    result = _scaffold_missing_docs(root, feat_path)
    assert result is None
    assert (feat_path / "CLAUDE.md").read_text(encoding="utf-8") == original


def test_features_without_claude_md(tmp_path):
    """Excludes features with CLAUDE.md, includes those without."""
    from scarlet.generator.context_yaml import features_without_claude_md

    root = _make_project(tmp_path, [("has_doc", True), ("no_doc", False), ("also_no_doc", False)])
    gaps = features_without_claude_md(root)

    assert "no_doc" in gaps
    assert "also_no_doc" in gaps
    assert "has_doc" not in gaps
