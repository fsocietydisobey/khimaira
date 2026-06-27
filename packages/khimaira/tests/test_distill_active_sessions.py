"""Tests for the pre-bake active-session distiller (settled-only + role guards)."""

from __future__ import annotations

import json
import os
import time

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
        project_root="/home/_3ntropy/dev/khimaira",
        project="khimaira",
        settle_min=30.0,
        recent_days=8.0,
        max_chars=50_000,
        dry_run=True,
        verbose=False,
    )
    assert s["distilled"] == []
    assert s["skipped"]["untracked"] == 1


# --- backfill: name recovery from transcript --------------------------------


def _write_transcript(proj_dir, sid, *, set_name=None, names=None, mtime_age_s=100 * 86400):
    """Write a synthetic transcript JSONL and set its mtime to now - mtime_age_s.

    `set_name`: emit one session_set_name tool_use with this name.
    `names`: emit several session_set_name tool_uses in order (last should win).
    """
    lines = [
        json.dumps(
            {"type": "user", "message": {"content": [{"type": "text", "text": "do the thing"}]}}
        )
    ]
    for nm in names or ([set_name] if set_name else []):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "mcp__khimaira__session_set_name",
                                "input": {"session_id": sid, "name": nm},
                            }
                        ]
                    },
                }
            )
        )
    # a decoy Skill invocation named like a skill — must NOT be mistaken for a name
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Skill",
                            "input": {"name": "khimaira-bootstrap-roster"},
                        }
                    ]
                },
            }
        )
    )
    path = proj_dir / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    t = time.time() - mtime_age_s
    os.utime(path, (t, t))
    return path


def test_name_from_transcript_finds_last_session_set_name(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    p = _write_transcript(proj, "sid1", names=["scratch", "khimaira-0"])
    assert das._name_from_transcript(p) == "khimaira-0"  # last self-naming wins


def test_name_from_transcript_none_when_absent_not_skill_conflation(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    # no session_set_name — only a Skill(name=...) decoy is present
    p = _write_transcript(proj, "sid2", set_name=None)
    assert das._name_from_transcript(p) is None


# --- backfill: selection + ledger -------------------------------------------


def _run_backfill(
    tmp_path,
    monkeypatch,
    *,
    tracked=None,
    confirm=False,
    backfill_since="",
    max_sessions=0,
    ledger=None,
):
    """Drive backfill against tmp_path's project dir. `tracked` = live session rows."""
    proj_dir = tmp_path / "-home--3ntropy-dev-khimaira"
    if not proj_dir.exists():
        proj_dir.mkdir(parents=True)
    monkeypatch.setattr(das, "_CLAUDE_PROJECTS", tmp_path)
    monkeypatch.setattr(das._sessions, "list_sessions", lambda **k: tracked or [])
    monkeypatch.setattr(das, "extract_transcript", lambda sid, **k: "transcript body")
    return das.distill_active_sessions(
        project_root="/home/_3ntropy/dev/khimaira",
        project="khimaira",
        settle_min=30.0,
        recent_days=8.0,
        max_chars=50_000,
        dry_run=False,
        verbose=False,
        backfill=True,
        backfill_since=backfill_since,
        max_sessions=max_sessions,
        confirm=confirm,
        ledger_path=ledger or (tmp_path / "ledger.json"),
    ), proj_dir


def test_backfill_distills_untracked_via_transcript_name(tmp_path, monkeypatch):
    s, proj = _run_backfill(tmp_path, monkeypatch)
    _write_transcript(proj, "ghost1", set_name="khimaira-0")  # untracked, names itself
    s, _ = _run_backfill(tmp_path, monkeypatch)  # re-run now that the file exists
    assert [d["domain"] for d in s["distilled"]] == ["khimaira:orchestration"]
    assert s["dry_run"] is True  # no --confirm → dry-run default


def test_backfill_skips_untracked_with_no_name(tmp_path, monkeypatch):
    s, proj = _run_backfill(tmp_path, monkeypatch)
    _write_transcript(proj, "ghost2", set_name=None)  # no session_set_name
    s, _ = _run_backfill(tmp_path, monkeypatch)
    assert s["distilled"] == []
    assert s["skipped"]["no_name"] == 1


def test_backfill_processes_stale_session_active_path_skips(tmp_path, monkeypatch):
    # A session in the registry but 100 days idle: active path = stale-skip; backfill picks it up.
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    _write_transcript(proj, "old1", set_name="backend-lead-1")
    tracked = [{"session_id": "old1", "name": "backend-lead-1", "last_active_age_s": 100 * 86400}]
    s, _ = _run_backfill(tmp_path, monkeypatch, tracked=tracked)
    assert [d["domain"] for d in s["distilled"]] == ["khimaira:backend"]


def test_backfill_ledger_idempotent_across_runs(tmp_path, monkeypatch):
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    _write_transcript(proj, "g3", set_name="khimaira-0")
    calls = []
    monkeypatch.setattr(
        das, "_mnemosyne_distill", lambda *a, **k: calls.append(a) or {"pairs_extracted": 4}
    )
    ledger = tmp_path / "ledger.json"
    s1, _ = _run_backfill(tmp_path, monkeypatch, confirm=True, ledger=ledger)
    assert len(s1["distilled"]) == 1 and len(calls) == 1
    assert ledger.exists()  # persisted
    s2, _ = _run_backfill(tmp_path, monkeypatch, confirm=True, ledger=ledger)
    assert s2["distilled"] == [] and s2["skipped"]["already_done"] == 1
    assert len(calls) == 1  # NOT re-distilled — ledger guarded


def test_backfill_dry_run_default_does_not_write(tmp_path, monkeypatch):
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    _write_transcript(proj, "g4", set_name="khimaira-0")
    calls = []
    monkeypatch.setattr(
        das, "_mnemosyne_distill", lambda *a, **k: calls.append(a) or {"pairs_extracted": 1}
    )
    ledger = tmp_path / "ledger.json"
    s, _ = _run_backfill(tmp_path, monkeypatch, confirm=False, ledger=ledger)
    assert len(s["distilled"]) == 1  # listed
    assert calls == []  # but distill NOT called
    assert not ledger.exists()  # and nothing ledgered


def test_backfill_since_excludes_older(tmp_path, monkeypatch):
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    _write_transcript(proj, "g5", set_name="khimaira-0", mtime_age_s=100 * 86400)
    # since = far in the future relative to the 100-day-old file → excluded
    s, _ = _run_backfill(tmp_path, monkeypatch, backfill_since="2099-01-01")
    assert s["distilled"] == []
    assert s["skipped"]["too_old"] == 1


def test_backfill_max_sessions_caps(tmp_path, monkeypatch):
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    for n in ("a", "b", "c"):
        _write_transcript(proj, f"cap{n}", set_name="khimaira-0")
    s, _ = _run_backfill(tmp_path, monkeypatch, max_sessions=1)
    assert len(s["distilled"]) == 1
    assert s["skipped"]["capped"] == 2


def test_backfill_live_guard_skips_recent_mtime(tmp_path, monkeypatch):
    proj = tmp_path / "-home--3ntropy-dev-khimaira"
    proj.mkdir(parents=True)
    # touched 60s ago < 30min settle → possibly mid-flight → skip
    _write_transcript(proj, "live1", set_name="khimaira-0", mtime_age_s=60)
    s, _ = _run_backfill(tmp_path, monkeypatch)
    assert s["distilled"] == []
    assert s["skipped"]["mid_flight"] == 1
