"""Tests for the pre-bake active-session distiller (settled-only + role guards)."""

from __future__ import annotations

from unittest.mock import patch

from khimaira.tools import distill_active_sessions as das


# --- pure helpers ----------------------------------------------------------


def test_encode_cwd_matches_claude_code_layout():
    # '/' and '_' both become '-' (verified against the live ~/.claude/projects dir).
    assert das._encode_cwd("/home/_3ntropy/dev/khimaira") == "-home--3ntropy-dev-khimaira"
    assert das._encode_cwd("/home/_3ntropy/dev/khimaira/") == "-home--3ntropy-dev-khimaira"


def test_resolve_domain_lead_gets_real_domain():
    assert das._resolve_domain("backend-lead-1") == "backend"
    assert das._resolve_domain("frontend-lead-2") == "frontend"


def test_resolve_domain_master_shapes_to_orchestration():
    assert das._resolve_domain("khimaira-0") == "orchestration"
    assert das._resolve_domain("master") == "orchestration"
    assert das._resolve_domain("jp-master-1") == "orchestration"


def test_resolve_domain_non_lead_non_master_skipped():
    for name in ("agent-1", "critic-1", "verifier-1", "analyst-1", "intake-1", "tracker-1"):
        assert das._resolve_domain(name) is None, name


def test_resolve_domain_named_master_via_master_names():
    # "muther" (jeevy roster master) isn't *-0/*master* shaped — needs explicit decl.
    assert das._resolve_domain("muther") is None  # without the hint
    assert das._resolve_domain("muther", frozenset({"muther"})) == "orchestration"


# --- the filter (settled / stale / low-value / role) -----------------------


def _run_filter(tmp_path, monkeypatch, rows, *, settle_min=30.0, recent_days=8.0):
    """Drive distill_active_sessions in dry-run against a temp transcript dir whose
    *.jsonl files correspond to `rows` (each row a session record)."""
    proj_dir = tmp_path / "-home--3ntropy-dev-khimaira"
    proj_dir.mkdir(parents=True)
    for r in rows:
        (proj_dir / f"{r['session_id']}.jsonl").write_text("{}\n")

    monkeypatch.setattr(das, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(das._sessions, "list_sessions", lambda **k: rows)
    # extract_transcript returns a non-empty body for any session with a file.
    monkeypatch.setattr(das, "extract_transcript", lambda sid, **k: "transcript body")

    return das.distill_active_sessions(
        project_root="/home/_3ntropy/dev/khimaira",
        project="khimaira",
        settle_min=settle_min,
        recent_days=recent_days,
        max_chars=50_000,
        dry_run=True,
        verbose=False,
    )


def test_settled_master_is_distilled(tmp_path, monkeypatch):
    rows = [{"session_id": "aaaa", "name": "khimaira-0", "last_active_age_s": 3600}]
    s = _run_filter(tmp_path, monkeypatch, rows)
    assert [d["domain"] for d in s["distilled"]] == ["khimaira:orchestration"]


def test_mid_flight_session_skipped(tmp_path, monkeypatch):
    # idle 5 min < 30 min settle floor → mid-flight, not distilled.
    rows = [{"session_id": "aaaa", "name": "khimaira-0", "last_active_age_s": 300}]
    s = _run_filter(tmp_path, monkeypatch, rows)
    assert s["distilled"] == []
    assert s["skipped"]["mid_flight"] == 1


def test_stale_session_skipped(tmp_path, monkeypatch):
    # idle 10 days > 8 day recency horizon → not this cycle's knowledge.
    rows = [{"session_id": "aaaa", "name": "backend-lead-1", "last_active_age_s": 10 * 86400}]
    s = _run_filter(tmp_path, monkeypatch, rows)
    assert s["distilled"] == []
    assert s["skipped"]["stale"] == 1


def test_low_value_role_skipped(tmp_path, monkeypatch):
    # settled + recent, but an agent → no durable-knowledge domain → skip.
    rows = [{"session_id": "aaaa", "name": "agent-2", "last_active_age_s": 3600}]
    s = _run_filter(tmp_path, monkeypatch, rows)
    assert s["distilled"] == []
    assert s["skipped"]["low_value"] == 1


def test_lead_distilled_to_its_domain(tmp_path, monkeypatch):
    rows = [{"session_id": "bbbb", "name": "frontend-lead-1", "last_active_age_s": 7200}]
    s = _run_filter(tmp_path, monkeypatch, rows)
    assert [d["domain"] for d in s["distilled"]] == ["khimaira:frontend"]


def test_untracked_transcript_skipped(tmp_path, monkeypatch):
    # A transcript file with no matching live session record → untracked, skipped.
    proj_dir = tmp_path / "-home--3ntropy-dev-khimaira"
    proj_dir.mkdir(parents=True)
    (proj_dir / "ghost.jsonl").write_text("{}\n")
    monkeypatch.setattr(das, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(das._sessions, "list_sessions", lambda **k: [])
    s = das.distill_active_sessions(
        project_root="/home/_3ntropy/dev/khimaira", project="khimaira",
        settle_min=30.0, recent_days=8.0, max_chars=50_000, dry_run=True, verbose=False,
    )
    assert s["distilled"] == []
    assert s["skipped"]["untracked"] == 1
