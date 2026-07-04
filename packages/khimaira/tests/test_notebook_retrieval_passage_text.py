"""Unit tests for notebook_retrieval._passage_text — deliberately SEPARATE
from test_notebook_retrieval.py (which is integration-marked and
module-skips entirely when qdrant isn't reachable at :6343). `_passage_text`
is a pure function with no qdrant/fastembed dependency, so its behavior
(including the sensitive-note redaction routing, a security property that
must always be checked regardless of qdrant availability) gets its own
unmarked, always-run test file.
"""

from __future__ import annotations

from khimaira.monitor import notebook_retrieval


def _record(
    *,
    raw_text: str = "raw",
    pipeline: dict | None = None,
    kind: str = "note",
    sensitive: bool = False,
    llm_text: str | None = None,
) -> dict:
    return {
        "id": "note-1",
        "raw_text": raw_text,
        "pipeline": pipeline,
        "kind": kind,
        "sensitive": sensitive,
        "llm_text": llm_text,
    }


def test_passage_text_note_with_pipeline_uses_summary_and_organized_md():
    record = _record(pipeline={"summary": "s", "organized_md": "m"})
    assert notebook_retrieval._passage_text(record) == "s\n\nm"


def test_passage_text_note_without_pipeline_falls_back_to_raw_text():
    record = _record(raw_text="unstructured draft")
    assert notebook_retrieval._passage_text(record) == "unstructured draft"


def test_passage_text_sensitive_note_without_pipeline_uses_redacted_twin():
    """BROKEN path this closes: the raw_text fallback for an unstructured
    note must never embed the real secret."""
    record = _record(
        raw_text="key: sk-ant-realvalue",
        sensitive=True,
        llm_text="key: ‹SECRET:anthropic_key#1›",
    )
    passage = notebook_retrieval._passage_text(record)
    assert "sk-ant-realvalue" not in passage
    assert passage == "key: ‹SECRET:anthropic_key#1›"


def test_passage_text_study_guide_uses_redacted_twin_for_body():
    """BROKEN path this closes: a guide's embed always includes raw_text[:N]
    — for a sensitive guide that must be the redacted twin."""
    record = _record(
        raw_text="# Guide\n\nkey: sk-ant-realvalue",
        pipeline={"abstract": "an abstract"},
        kind="study_guide",
        sensitive=True,
        llm_text="# Guide\n\nkey: ‹SECRET:anthropic_key#1›",
    )
    passage = notebook_retrieval._passage_text(record)
    assert "sk-ant-realvalue" not in passage
    assert "an abstract" in passage
    assert "‹SECRET:anthropic_key#1›" in passage


def test_passage_text_non_sensitive_note_unaffected():
    record = _record(raw_text="plain text, no secrets")
    assert notebook_retrieval._passage_text(record) == "plain text, no secrets"
