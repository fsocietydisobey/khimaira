"""Tests for khimaira.monitor.notebook_pipeline (Phase 1c).

The `claude -p` subprocess is mocked throughout — these tests exercise the
deterministic parse/retry/tollgate logic, not the real CLI. Canned envelopes
mirror `claude -p --output-format json`'s shape: {"result": "<json string>", ...}.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


@pytest.fixture
def pipeline(notes_store, monkeypatch):
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    importlib.reload(pipeline_mod)
    # Zero out retry backoff so retry-path tests don't burn real wall-clock
    # time — this patches the module's own constant, not asyncio.sleep
    # itself, so it doesn't affect the unrelated asyncio.sleep(0.05) used
    # elsewhere in this file to let a background task run to completion.
    monkeypatch.setattr(pipeline_mod, "_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    # Default the LEAF dependencies of answer_question's code-grounding step
    # to "nothing available" — most tests in this file default a note's repo
    # to "khimaira" (notes.py's _DEFAULT_REPO), which is the REAL monorepo
    # this test suite runs inside. Without this, every answer_question test
    # would hit a real Séance import/API call (or, on failure, a real
    # ripgrep subprocess against this actual repo) — slow, network-
    # dependent, non-deterministic. Patching the leaves (not
    # _code_grounding_for_repo itself) keeps that function's own real logic
    # exercised by every test, and lets the tests that exercise it directly
    # override these two per-test as needed.
    monkeypatch.setattr(
        pipeline_mod, "_seance_code_search", lambda repo, question: ([], False, False)
    )
    monkeypatch.setattr(pipeline_mod, "_repo_root", lambda repo: None)
    yield pipeline_mod


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input=None):
        return self._stdout, self._stderr


def _queue_responses(pipeline, monkeypatch, responses: list[_FakeProc]):
    """Patch asyncio.create_subprocess_exec to pop canned responses in order.

    Only intercepts the `claude` invocation — `git` calls (revalidate_note's
    staleness-gate/sha lookups) pass through to the REAL subprocess so tests
    exercise real git behavior against the disposable git_repo fixture.
    """
    queue = list(responses)
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        if args and args[0] == "git":
            return await real_exec(*args, **kwargs)
        return queue.pop(0)

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)


_VALID_PAYLOAD = {
    "title": "A test note title",
    "summary": "a summary",
    "technical": "tech details",
    "plain": "plain english",
    "organized_md": "# organized",
    "tags": ["fix", "backend"],
    "entities": ["notes.py"],
}


def _envelope(result_str: str) -> bytes:
    return json.dumps({"result": result_str, "session_id": "irrelevant"}).encode("utf-8")


async def test_transform_note_success(pipeline, monkeypatch):
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_strips_markdown_fence(pipeline, monkeypatch):
    fenced = "```json\n" + json.dumps(_VALID_PAYLOAD) + "\n```"
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(fenced))])
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_retries_once_then_succeeds(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("not json at all")),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_fails_after_max_bad_attempts(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("not json at all")),
            _FakeProc(_envelope("still not json")),
            _FakeProc(_envelope("still not json either")),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result is None


async def test_transform_note_backs_off_between_attempts(pipeline, monkeypatch):
    """The retry/backoff fix (2026-07-03, superseding the original 'retry
    once' spec): failed attempts sleep between tries (giving the CLI time
    to warm up), and there's no trailing sleep after a successful attempt."""
    monkeypatch.setattr(pipeline, "_RETRY_BACKOFF_SECONDS", (0.1, 0.2, 0.3))
    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(pipeline.asyncio, "sleep", fake_sleep)
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("bad")),
            _FakeProc(_envelope("bad again")),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD
    assert sleep_calls == [0.1, 0.2]


# ---------------------------------------------------------------------------
# Grimoire Phase 4 addendum (2026-07-04): bounded structuring concurrency —
# a bulk import schedules one task per guide with no cap of its own, so the
# cap must live at _invoke_claude's chokepoint (every structuring/organize/
# revalidate/ask-synthesis call funnels through it).
# ---------------------------------------------------------------------------


def test_structure_concurrency_defaults_to_three(pipeline):
    assert pipeline._STRUCTURE_CONCURRENCY == 3


def test_structure_concurrency_reads_env_override(notes_store, monkeypatch):
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    monkeypatch.setenv("KHIMAIRA_NOTEBOOK_STRUCTURE_CONCURRENCY", "7")
    importlib.reload(pipeline_mod)
    assert pipeline_mod._STRUCTURE_CONCURRENCY == 7


async def test_invoke_claude_concurrency_is_bounded_by_semaphore(pipeline, monkeypatch):
    """N > bound concurrent _invoke_claude calls must never have more than
    `bound` subprocess spawns in flight at once — the actual safety property
    the semaphore exists for."""
    bound = 2
    monkeypatch.setattr(pipeline, "_STRUCTURE_SEMAPHORE", asyncio.Semaphore(bound))

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_exec(*args, **kwargs):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)

    results = await asyncio.gather(
        *[pipeline._invoke_claude("content", "instruction") for _ in range(6)]
    )

    assert peak == bound
    assert len(results) == 6


async def test_invoke_claude_agentic_is_not_gated_by_structure_semaphore(pipeline, monkeypatch):
    """Research calls are a separate, user-serial code path — they must NOT
    queue behind the structuring semaphore (that would make an ask wait on
    an unrelated bulk import draining). Proven by forcing the structuring
    gate down to bound=1 and confirming two agentic calls STILL overlap —
    if they were (bugged into) sharing it, peak concurrency would be capped
    at 1."""
    monkeypatch.setattr(pipeline, "_STRUCTURE_SEMAPHORE", asyncio.Semaphore(1))

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_exec(*args, **kwargs):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return _FakeProc(_stream_stdout(_result_event('{"answer":"x"}')))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)

    await asyncio.gather(
        pipeline._invoke_claude_agentic("q1", "i"),
        pipeline._invoke_claude_agentic("q2", "i"),
    )

    assert peak == 2


async def test_transform_note_validation_failure_retries(pipeline, monkeypatch):
    """A well-formed JSON object missing required keys must also retry, not crash."""
    incomplete = {"summary": "only this key"}
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope(json.dumps(incomplete))),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_nonzero_exit_retries(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(b"", stderr=b"boom", returncode=1),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_trigger_pipeline_success_sets_processed(notes_store, pipeline, monkeypatch):
    _stub_organize_after_structuring(monkeypatch)
    note = notes_store.add_note("some raw content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])

    await pipeline.trigger_pipeline(note["id"])

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "processed"
    # title is popped out of the stored pipeline and promoted to the note's
    # top-level display title — see trigger_pipeline.
    assert updated["pipeline"] == {k: v for k, v in _VALID_PAYLOAD.items() if k != "title"}
    assert updated["title"] == _VALID_PAYLOAD["title"]
    assert updated["raw_text"] == "some raw content"


async def test_trigger_pipeline_sensitive_note_feeds_redacted_text_not_raw(
    notes_store, pipeline, monkeypatch
):
    """Sensitive notes (2026-07-04): the structuring subprocess must receive
    llm_view(record) via stdin, never the real raw_text."""
    _stub_organize_after_structuring(monkeypatch)
    secret = "sk-ant-" + "a" * 30
    note = notes_store.add_note(f"API_KEY={secret}", sensitive=True)
    seen_stdin: list[str] = []

    class _CapturingProc:
        def __init__(self, stdout):
            self._stdout = stdout
            self.returncode = 0

        async def communicate(self, input=None):
            seen_stdin.append(input.decode("utf-8"))
            return self._stdout, b""

    async def fake_exec(*args, **kwargs):
        return _CapturingProc(_envelope(json.dumps(_VALID_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)

    await pipeline.trigger_pipeline(note["id"])

    assert len(seen_stdin) == 1
    assert secret not in seen_stdin[0]


async def test_trigger_pipeline_failure_marks_failed_and_preserves_raw_text(
    notes_store, pipeline, monkeypatch
):
    note = notes_store.add_note("irreplaceable raw content")
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("garbage")),
            _FakeProc(_envelope("still garbage")),
            _FakeProc(_envelope("still garbage too")),
        ],
    )

    await pipeline.trigger_pipeline(note["id"])

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "failed"
    assert updated["pipeline"] is None
    assert updated["raw_text"] == "irreplaceable raw content"


async def test_trigger_pipeline_note_deleted_before_transform_is_a_noop(
    notes_store, pipeline, monkeypatch
):
    # No note created — note_id resolves to nothing. Must not raise.
    await pipeline.trigger_pipeline("no-such-note")


async def test_trigger_pipeline_calls_organize_after_structuring(
    pipeline, notes_store, monkeypatch
):
    """2026-07-04: the organizer was extended to regular notes — a
    successful note structuring pass now fires the SAME post-structuring
    hook the guide pipeline already had."""
    from khimaira.monitor import notebook_organizer

    note = notes_store.add_note("some raw content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])
    calls: list[str] = []

    async def fake_organize_after_structuring(note_id):
        calls.append(note_id)

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", fake_organize_after_structuring
    )

    await pipeline.trigger_pipeline(note["id"])

    assert calls == [note["id"]]


async def test_trigger_pipeline_skip_organize_suppresses_the_hook(
    pipeline, notes_store, monkeypatch
):
    from khimaira.monitor import notebook_organizer

    note = notes_store.add_note("some raw content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])
    calls: list[str] = []

    async def fake_organize_after_structuring(note_id):
        calls.append(note_id)

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", fake_organize_after_structuring
    )

    await pipeline.trigger_pipeline(note["id"], skip_organize=True)

    assert calls == []
    assert notes_store.get_note(note["id"])["status"] == "processed"  # structuring still ran


async def test_schedule_pipeline_creates_background_task(notes_store, pipeline, monkeypatch):
    note = notes_store.add_note("scheduled content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])

    pipeline.schedule_pipeline(note["id"])
    # Let the scheduled task run to completion.
    await asyncio.sleep(0.05)

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "processed"


# ---------------------------------------------------------------------------
# revalidate_note (Phase 2a — north-star self-healing)
#
# Uses a REAL disposable git repo (fast local binary, no network) so the
# staleness gate's `git diff` logic is exercised for real; only the `claude
# -p` subprocess is mocked.
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, check=True, capture_output=True)


def _git_head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "sessions.py").write_text("def foo():\n    pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _patch_repo_root(pipeline, monkeypatch, repo_name, repo_path):
    monkeypatch.setattr(
        pipeline, "_repo_root", lambda repo: repo_path if repo == repo_name else None
    )


_ANCHORED_PAYLOAD = {**_VALID_PAYLOAD, "entities": ["sessions.py"]}


async def test_revalidate_note_staleness_gate_skips_when_unchanged(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    # No canned responses queued — an LLM call here would raise IndexError.
    _queue_responses(pipeline, monkeypatch, [])

    result = await pipeline.revalidate_note(note["id"])
    assert result["validated_git_sha"] == initial_sha
    assert result["history"] == []
    assert result["pipeline"] == _ANCHORED_PAYLOAD


async def test_revalidate_note_heals_when_anchor_file_changed(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    return 42\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "change")
    new_sha = _git_head(git_repo)

    healed_payload = {**_ANCHORED_PAYLOAD, "summary": "updated summary", "unchanged": False}
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(healed_payload)))])

    result = await pipeline.revalidate_note(note["id"])
    assert result["pipeline"]["summary"] == "updated summary"
    assert result["validated_git_sha"] == new_sha
    assert len(result["history"]) == 1
    assert result["history"][0]["pipeline"] == _ANCHORED_PAYLOAD
    assert result["history"][0]["validated_git_sha"] == initial_sha
    assert result["raw_text"] == "raw"


async def test_revalidate_note_llm_confirms_unchanged_skips_history(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    pass  # cosmetic\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "cosmetic")
    new_sha = _git_head(git_repo)

    # LLM re-checks (anchor changed) but judges the note still accurate.
    _queue_responses(
        pipeline,
        monkeypatch,
        [_FakeProc(_envelope(json.dumps({**_ANCHORED_PAYLOAD, "unchanged": True})))],
    )

    result = await pipeline.revalidate_note(note["id"])
    assert result["pipeline"] == _ANCHORED_PAYLOAD
    assert result["history"] == []
    assert result["validated_git_sha"] == new_sha


async def test_revalidate_note_backfills_title_even_when_unchanged(
    notes_store, pipeline, monkeypatch, git_repo
):
    """Title backfill (Joseph, 2026-07-03) applies on ANY revalidate pass
    that reaches the LLM, independent of the unchanged/healed decision —
    unlike the rest of the pipeline fields, which are discarded when
    unchanged=true."""
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo", title="old truncated title")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    pass  # cosmetic\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "cosmetic")

    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(
                _envelope(
                    json.dumps(
                        {**_ANCHORED_PAYLOAD, "title": "Fresh backfilled title", "unchanged": True}
                    )
                )
            )
        ],
    )

    result = await pipeline.revalidate_note(note["id"])
    assert result["title"] == "Fresh backfilled title"
    assert result["history"] == []  # still a no-op on content — just the title moved
    assert result["pipeline"] == _ANCHORED_PAYLOAD  # title never lands inside pipeline


async def test_revalidate_note_unchanged_true_skips_history_despite_reworded_text(
    notes_store, pipeline, monkeypatch, git_repo
):
    """Regression: revalidate_note used to infer "healed" from dict equality
    against the model's own regenerated JSON. Real LLM calls never reproduce
    a free-text field (organized_md especially) byte-for-byte even when the
    model's own judgment is "still accurate" — so a genuinely unchanged note
    got a spurious history entry + "healed" badge on nearly every revalidation
    that went through the LLM path. The explicit `unchanged` flag must be what
    decides this, not equality — so a reworded-but-unchanged=true response
    must NOT create a history entry."""
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    pass  # cosmetic\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "cosmetic")

    reworded_but_unchanged = {
        **_ANCHORED_PAYLOAD,
        "organized_md": "# organized (slightly different wording this time)",
        "unchanged": True,
    }
    _queue_responses(
        pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(reworded_but_unchanged)))]
    )

    result = await pipeline.revalidate_note(note["id"])
    assert result["history"] == []
    assert result["pipeline"] == _ANCHORED_PAYLOAD  # untouched — reworded text discarded


async def test_revalidate_note_no_file_entities_never_gate_skips(
    notes_store, pipeline, monkeypatch, git_repo
):
    """Entities that aren't file-shaped resolve to zero anchor files — nothing
    to diff, so the gate always pays for the LLM re-check."""
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    conceptual_payload = {**_VALID_PAYLOAD, "entities": ["session reaper", "lock"]}
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], conceptual_payload)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(note["id"], git_sha=initial_sha, new_pipeline=None)

    _queue_responses(
        pipeline,
        monkeypatch,
        [_FakeProc(_envelope(json.dumps({**conceptual_payload, "unchanged": True})))],
    )
    result = await pipeline.revalidate_note(note["id"])
    assert result["pipeline"] == conceptual_payload


async def test_revalidate_note_llm_failure_leaves_record_unchanged(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_note("raw", repo="testrepo")
    notes_store.set_pipeline(note["id"], _ANCHORED_PAYLOAD)
    # No prior validation — gate check is skipped, goes straight to the LLM.

    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("garbage")),
            _FakeProc(_envelope("still garbage")),
            _FakeProc(_envelope("still garbage too")),
        ],
    )
    result = await pipeline.revalidate_note(note["id"])
    assert result["pipeline"] == _ANCHORED_PAYLOAD
    assert result["validated_git_sha"] is None
    assert result["last_validated_at"] is None


async def test_revalidate_note_unknown_repo_returns_unchanged(notes_store, pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: None)
    note = notes_store.add_note("raw", repo="nonexistent-repo")
    result = await pipeline.revalidate_note(note["id"])
    assert result["validated_git_sha"] is None


async def test_revalidate_note_general_repo_returns_as_is(pipeline, notes_store, monkeypatch):
    """General-bucket notes (no codebase) skip validation entirely — no
    _repo_root lookup, no git, no LLM call, no warning."""
    repo_root_called = []
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: repo_root_called.append(repo))
    note = notes_store.add_note("raw", repo=notes_store.GENERAL_REPO)

    result = await pipeline.revalidate_note(note["id"])
    assert result["id"] == note["id"]
    assert result["validated_git_sha"] is None
    assert repo_root_called == []


def test_repo_root_resolves_via_real_project_registry(pipeline):
    """Regression: _repo_root's internal import path was wrong (khimaira.discovery
    vs the real khimaira.monitor.discovery) and every other revalidate_note test
    mocks _repo_root itself, so none of them would have caught it. This test
    exercises the REAL function against the REAL registry — no mocking.

    The `pipeline` fixture stubs _repo_root to None by default (a safety net
    for answer_question's code-grounding step, see the fixture), so this
    test reloads the module fresh to get the real, unpatched function back."""
    from pathlib import Path

    importlib.reload(pipeline)
    root = pipeline._repo_root("khimaira")
    assert root is not None
    assert Path(root) == Path(__file__).resolve().parents[3]
    assert pipeline._repo_root("no-such-repo-xyz") is None


async def test_revalidate_note_not_a_git_checkout_returns_unchanged(
    notes_store, pipeline, monkeypatch, tmp_path
):
    non_git_dir = tmp_path / "plain"
    non_git_dir.mkdir()
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: non_git_dir)
    note = notes_store.add_note("raw", repo="plain-repo")
    result = await pipeline.revalidate_note(note["id"])
    assert result["validated_git_sha"] is None


async def test_revalidate_note_unknown_id_raises(pipeline):
    with pytest.raises(ValueError, match="No note with id"):
        await pipeline.revalidate_note("no-such-note")


# ---------------------------------------------------------------------------
# Grimoire Phase 2: guide currency = a DRIFT REPORT, not a full pipeline
# regeneration — only `abstract` is ever regenerated; `toc`/`tags`/`entities`
# and `title`/`raw_text` are never touched. A separate concern from
# notebook_organizer's collection placement.
# ---------------------------------------------------------------------------

_GUIDE_ANCHORED_PIPELINE = {
    "abstract": "original abstract",
    "toc": [{"title": "Widgets", "anchor": "widgets", "level": 1}],
    "tags": ["widgets"],
    "entities": ["sessions.py"],
}


async def test_revalidate_note_study_guide_staleness_gate_skips_when_unchanged(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    guide = notes_store.add_study_guide("# Widgets\n\nbody", repo="testrepo")
    notes_store.set_study_guide_pipeline(guide["id"], _GUIDE_ANCHORED_PIPELINE)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(guide["id"], git_sha=initial_sha, new_pipeline=None)

    _queue_responses(pipeline, monkeypatch, [])  # no LLM call expected

    result = await pipeline.revalidate_note(guide["id"])
    assert result["validated_git_sha"] == initial_sha
    assert result["history"] == []
    assert result["pipeline"] == _GUIDE_ANCHORED_PIPELINE


async def test_revalidate_note_study_guide_drift_regenerates_abstract_only(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    guide = notes_store.add_study_guide("# Widgets\n\nbody", repo="testrepo")
    notes_store.set_study_guide_pipeline(guide["id"], _GUIDE_ANCHORED_PIPELINE)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(guide["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    return 42\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "change")
    new_sha = _git_head(git_repo)

    drift_response = {"abstract": "corrected abstract", "unchanged": False}
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(drift_response)))])

    result = await pipeline.revalidate_note(guide["id"])

    assert result["pipeline"]["abstract"] == "corrected abstract"
    assert result["pipeline"]["toc"] == _GUIDE_ANCHORED_PIPELINE["toc"]  # untouched
    assert result["pipeline"]["tags"] == _GUIDE_ANCHORED_PIPELINE["tags"]  # untouched
    assert result["pipeline"]["entities"] == _GUIDE_ANCHORED_PIPELINE["entities"]  # untouched
    assert result["validated_git_sha"] == new_sha
    assert result["title"] == guide["title"]  # never LLM-touched
    assert result["raw_text"] == "# Widgets\n\nbody"  # never touched
    assert len(result["history"]) == 1
    assert result["history"][0]["pipeline"] == _GUIDE_ANCHORED_PIPELINE


async def test_revalidate_note_study_guide_unchanged_stamps_without_history_churn(
    notes_store, pipeline, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    guide = notes_store.add_study_guide("# Widgets\n\nbody", repo="testrepo")
    notes_store.set_study_guide_pipeline(guide["id"], _GUIDE_ANCHORED_PIPELINE)
    initial_sha = _git_head(git_repo)
    notes_store.apply_validation(guide["id"], git_sha=initial_sha, new_pipeline=None)

    (git_repo / "sessions.py").write_text("def foo():\n    pass  # cosmetic\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "cosmetic")
    new_sha = _git_head(git_repo)

    _queue_responses(
        pipeline,
        monkeypatch,
        [_FakeProc(_envelope(json.dumps({"abstract": "corrected abstract", "unchanged": True})))],
    )

    result = await pipeline.revalidate_note(guide["id"])
    assert result["pipeline"] == _GUIDE_ANCHORED_PIPELINE  # unchanged -> discarded
    assert result["validated_git_sha"] == new_sha
    assert result["history"] == []


async def test_revalidate_note_study_guide_uses_higher_anchor_cap(
    notes_store, pipeline, monkeypatch
):
    """Guides get _MAX_ANCHOR_FILES_GUIDE (15), not the note cap (5)."""
    guide = notes_store.add_study_guide("# Widgets\n\nbody", repo="testrepo")
    notes_store.set_study_guide_pipeline(guide["id"], _GUIDE_ANCHORED_PIPELINE)

    seen_caps: list[int] = []

    def fake_resolve_anchor_files(repo_root, entities, cap):
        seen_caps.append(cap)
        return []

    async def fake_current_git_sha(repo_root):
        return "deadbeef"

    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: "/fake/repo")
    monkeypatch.setattr(pipeline, "_current_git_sha", fake_current_git_sha)
    monkeypatch.setattr(pipeline, "_resolve_anchor_files", fake_resolve_anchor_files)
    _queue_responses(
        pipeline,
        monkeypatch,
        [_FakeProc(_envelope(json.dumps({"abstract": "x", "unchanged": True})))],
    )

    await pipeline.revalidate_note(guide["id"])

    assert seen_caps == [pipeline._MAX_ANCHOR_FILES_GUIDE]


# ---------------------------------------------------------------------------
# answer_question (Phase 2c — the ask-layer capstone)
#
# search_notes_async + revalidate_note are mocked directly here — the real
# git/claude machinery behind revalidate_note is already covered above.
# These tests exercise the orchestration: retrieve -> revalidate-each ->
# synthesize, plus the empty/skip/failure edge cases.
# ---------------------------------------------------------------------------


async def test_answer_question_no_hits_returns_no_notes_found(pipeline, notes_store, monkeypatch):
    async def fake_search(query, **kwargs):
        return []

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)

    result = await pipeline.answer_question("anything")
    assert result == {
        "answer": "No relevant notes found.",
        "sources": [],
        "healed": [],
        "code_sources": [],
        "code_unavailable": [],
    }


async def test_answer_question_orchestrates_retrieve_revalidate_synth(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)  # no heal — just current

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("The answer is 42."))])

    result = await pipeline.answer_question("what is the answer")
    assert result["answer"] == "The answer is 42."
    assert result["sources"] == [note["id"]]
    assert result["healed"] == []


async def test_answer_question_sensitive_unstructured_note_falls_back_to_redacted_text(
    pipeline, notes_store, monkeypatch
):
    """Sensitive notes (2026-07-04): an UNSTRUCTURED sensitive note (no
    pipeline yet) falls back to raw_text in the ask-synthesis prompt — that
    fallback must route through llm_view, never the real secret. The note
    body lands in the --append-system-prompt arg (via _ASK_INSTRUCTION_
    TEMPLATE), not stdin (which only ever carries the bare question)."""
    secret = "sk-ant-" + "p" * 30
    note = notes_store.add_note(f"key: {secret}", tab_id="t1", sensitive=True)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)

    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        return _FakeProc(_envelope("some answer"))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)

    result = await pipeline.answer_question("what is the key?")

    assert result["sources"] == [note["id"]]
    assert len(captured_args) == 1
    system_prompt_idx = captured_args[0].index("--append-system-prompt") + 1
    assert secret not in captured_args[0][system_prompt_idx]


async def test_answer_question_tracks_healed_notes(pipeline, notes_store, monkeypatch):
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.apply_validation(
            note_id, git_sha="deadbeef", new_pipeline={**_VALID_PAYLOAD, "summary": "healed"}
        )

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("healed answer"))])

    result = await pipeline.answer_question("question")
    assert result["healed"] == [note["id"]]
    assert result["sources"] == [note["id"]]


async def test_answer_question_uses_healed_content_in_synthesis(pipeline, notes_store, monkeypatch):
    """The stale hit must be healed BEFORE its content is fed to the synth
    step — this proves the ordering, not just the healed[] bookkeeping."""
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(
        note["id"], {**_VALID_PAYLOAD, "summary": "STALE summary", "organized_md": "STALE body"}
    )

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.apply_validation(
            note_id,
            git_sha="sha2",
            new_pipeline={
                **_VALID_PAYLOAD,
                "summary": "HEALED summary",
                "organized_md": "HEALED body",
            },
        )

    captured: dict[str, str] = {}

    async def fake_invoke(content, instruction):
        captured["instruction"] = instruction
        return "final answer"

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    monkeypatch.setattr(pipeline, "_invoke_claude", fake_invoke)

    result = await pipeline.answer_question("q")
    assert "HEALED body" in captured["instruction"]
    assert "STALE body" not in captured["instruction"]
    assert result["healed"] == [note["id"]]


async def test_answer_question_skips_hit_whose_note_vanished(pipeline, notes_store, monkeypatch):
    async def fake_search(query, **kwargs):
        return [{"note_id": "no-such-note", "score": 0.9}]

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)

    result = await pipeline.answer_question("q")
    assert result == {
        "answer": "No relevant notes found.",
        "sources": [],
        "healed": [],
        "code_sources": [],
        "code_unavailable": [],
    }


async def test_answer_question_synthesis_failure_still_returns_sources(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    async def fake_invoke(content, instruction):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    monkeypatch.setattr(pipeline, "_invoke_claude", fake_invoke)

    result = await pipeline.answer_question("q")
    assert result["sources"] == [note["id"]]
    assert "couldn't synthesize" in result["answer"].lower()


async def test_answer_question_repo_filter_passed_through(pipeline, notes_store, monkeypatch):
    captured_kwargs: dict = {}

    async def fake_search(query, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)

    await pipeline.answer_question("q", repo="jeevy_portal")
    assert captured_kwargs.get("repo") == "jeevy_portal"


# ---------------------------------------------------------------------------
# ask-layer v2: Séance code-grounding + grep fallback + @-mention plumbing
#
# _seance_code_search tests patch the seance package's own symbols (its
# imports are local/lazy inside the function, so patching e.g.
# "seance.config.load_config" before the call is correctly picked up by the
# fresh `from seance.config import load_config` at call time).
# ---------------------------------------------------------------------------


class _FakeSearchResult:
    def __init__(self, **kwargs):
        self._data = kwargs

    def to_dict(self):
        return dict(self._data)


def test_seance_code_search_not_indexed_skips_the_embed_call(pipeline, monkeypatch):
    """A not-indexed repo must short-circuit before SearchEngine.search() —
    that call hits a real embedding API, so paying for it on a guaranteed
    miss would be wasteful.

    Reloads first: the `pipeline` fixture stubs _seance_code_search itself
    (a safety net for other tests, see the fixture) — this test exercises
    the REAL function, so it needs that stub undone first."""
    importlib.reload(pipeline)
    search_called = []

    class _FakeVectorStore:
        def __init__(self, config):
            pass

        def list_projects(self):
            return [{"name": "some_other_repo", "chunks": 10}]

    class _FakeSearchEngine:
        def __init__(self, config):
            pass

        def search(self, **kwargs):
            search_called.append(kwargs)
            return []

    monkeypatch.setattr("seance.config.load_config", lambda: object())
    monkeypatch.setattr("seance.storage.vectordb.VectorStore", _FakeVectorStore)
    monkeypatch.setattr("seance.search.engine.SearchEngine", _FakeSearchEngine)

    chunks, indexed, errored = pipeline._seance_code_search("my-repo", "question")
    assert chunks == []
    assert indexed is False
    assert errored is False
    assert search_called == []


def test_seance_code_search_indexed_returns_results(pipeline, monkeypatch):
    importlib.reload(pipeline)  # undo the fixture's _seance_code_search stub — see above

    class _FakeVectorStore:
        def __init__(self, config):
            pass

        def list_projects(self):
            return [{"name": "my_repo", "chunks": 42}]

    class _FakeSearchEngine:
        def __init__(self, config):
            pass

        def search(self, *, project_name, query, top_k):
            return [
                _FakeSearchResult(
                    file_path="foo.py",
                    symbol_name="bar",
                    chunk_type="function",
                    language="python",
                    start_line=1,
                    end_line=5,
                    score=0.1,
                    text="def bar(): pass",
                )
            ]

    monkeypatch.setattr("seance.config.load_config", lambda: object())
    monkeypatch.setattr("seance.storage.vectordb.VectorStore", _FakeVectorStore)
    monkeypatch.setattr("seance.search.engine.SearchEngine", _FakeSearchEngine)

    chunks, indexed, errored = pipeline._seance_code_search("my_repo", "question")
    assert indexed is True
    assert errored is False
    assert chunks[0]["file_path"] == "foo.py"


def test_seance_code_search_missing_api_key_systemexit_is_caught(pipeline, monkeypatch):
    """Regression: seance.config.load_config() raises SystemExit (a
    BaseException, not Exception) when GOOGLE_AI_API_KEY is unset — must be
    caught explicitly, not propagate and break the whole ask."""
    importlib.reload(pipeline)  # undo the fixture's _seance_code_search stub — see above

    def _raise_system_exit():
        raise SystemExit("GOOGLE_AI_API_KEY is not set.")

    monkeypatch.setattr("seance.config.load_config", _raise_system_exit)

    chunks, indexed, errored = pipeline._seance_code_search("any-repo", "question")
    assert chunks == []
    assert indexed is False
    assert errored is True


def test_grep_code_fallback_finds_matching_files(pipeline, tmp_path):
    (tmp_path / "widget.py").write_text("def frobnicate_widget():\n    return 42\n")
    (tmp_path / "unrelated.py").write_text("def other():\n    pass\n")

    chunks = pipeline._grep_code_fallback(tmp_path, "how does frobnicate widget work")
    assert any("widget.py" in c["file_path"] for c in chunks)
    assert not any("unrelated.py" in c["file_path"] for c in chunks)


async def test_code_grounding_trusts_indexed_seance_even_when_empty(pipeline, monkeypatch):
    """Indexed-but-zero-hits is a valid 'no match', not 'unavailable' — must
    not trigger a grep fallback."""
    monkeypatch.setattr(pipeline, "_seance_code_search", lambda repo, question: ([], True, False))

    chunks, unavailable = await pipeline._code_grounding_for_repo("my-repo", "q")
    assert chunks == []
    assert unavailable is False


async def test_code_grounding_falls_back_to_grep_when_not_indexed(pipeline, monkeypatch, tmp_path):
    (tmp_path / "widget.py").write_text("def frobnicate_widget():\n    return 42\n")
    monkeypatch.setattr(pipeline, "_seance_code_search", lambda repo, question: ([], False, False))
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: tmp_path)

    chunks, unavailable = await pipeline._code_grounding_for_repo("my-repo", "frobnicate widget")
    assert unavailable is False
    assert any("widget.py" in c["file_path"] for c in chunks)
    assert all(c["repo"] == "my-repo" for c in chunks)


async def test_code_grounding_unavailable_when_repo_root_unresolvable(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, "_seance_code_search", lambda repo, question: ([], False, True))
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: None)

    chunks, unavailable = await pipeline._code_grounding_for_repo("no-such-repo", "q")
    assert chunks == []
    assert unavailable is True


async def test_code_grounding_unavailable_when_grep_finds_nothing(pipeline, monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "_seance_code_search", lambda repo, question: ([], False, False))
    monkeypatch.setattr(pipeline, "_repo_root", lambda repo: tmp_path)

    chunks, unavailable = await pipeline._code_grounding_for_repo("my-repo", "zzzznomatchzzzz")
    assert chunks == []
    assert unavailable is True


async def test_answer_question_mentioned_notes_prioritized_and_deduped(
    pipeline, notes_store, monkeypatch
):
    mentioned = notes_store.add_note("mentioned raw", tab_id="t1")
    notes_store.set_pipeline(mentioned["id"], _VALID_PAYLOAD)
    retrieved = notes_store.add_note("retrieved raw", tab_id="t1")
    notes_store.set_pipeline(retrieved["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        # Retrieval also surfaces the mentioned note again — must dedup.
        return [
            {"note_id": mentioned["id"], "score": 0.5},
            {"note_id": retrieved["id"], "score": 0.4},
        ]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("answer"))])

    result = await pipeline.answer_question("q", mentioned_note_ids=[mentioned["id"]])
    assert result["sources"] == [mentioned["id"], retrieved["id"]]


async def test_answer_question_exclusive_skips_retrieval(pipeline, notes_store, monkeypatch):
    mentioned = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(mentioned["id"], _VALID_PAYLOAD)

    search_called: list[str] = []

    async def fake_search(query, **kwargs):
        search_called.append(query)
        return [{"note_id": "should-not-be-used", "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("answer"))])

    result = await pipeline.answer_question(
        "q", mentioned_note_ids=[mentioned["id"]], exclusive=True
    )
    assert result["sources"] == [mentioned["id"]]
    assert search_called == []


async def test_answer_question_includes_code_section_and_sources(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    async def fake_grounding(repo, question):
        return [
            {
                "repo": repo,
                "file_path": "foo.py",
                "start_line": 1,
                "end_line": 3,
                "symbol_name": "bar",
                "text": "def bar(): ...",
            }
        ], False

    captured: dict[str, str] = {}

    async def fake_invoke(content, instruction):
        captured["instruction"] = instruction
        return "final answer"

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    monkeypatch.setattr(pipeline, "_code_grounding_for_repo", fake_grounding)
    monkeypatch.setattr(pipeline, "_invoke_claude", fake_invoke)

    result = await pipeline.answer_question("q")
    assert result["code_sources"] == [
        {"repo": "khimaira", "file_path": "foo.py", "start_line": 1, "end_line": 3}
    ]
    assert result["code_unavailable"] == []
    assert "foo.py" in captured["instruction"]
    assert "RELEVANT CODE" in captured["instruction"]


async def test_answer_question_code_unavailable_surfaced(pipeline, notes_store, monkeypatch):
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    async def fake_grounding(repo, question):
        return [], True

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    monkeypatch.setattr(pipeline, "_code_grounding_for_repo", fake_grounding)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("answer"))])

    result = await pipeline.answer_question("q")
    assert result["code_unavailable"] == ["khimaira"]
    assert result["code_sources"] == []


async def test_answer_question_skips_code_grounding_for_general_repo(
    pipeline, notes_store, monkeypatch
):
    """General-bucket notes have no codebase — code-grounding must not even
    be attempted (not attempted ≠ unavailable; General shouldn't show up in
    code_unavailable either, since that field means 'tried and failed')."""
    note = notes_store.add_note("raw", tab_id="t1", repo=notes_store.GENERAL_REPO)
    notes_store.set_pipeline(note["id"], _VALID_PAYLOAD)

    async def fake_search(query, **kwargs):
        return [{"note_id": note["id"], "score": 0.9}]

    async def fake_revalidate(note_id):
        return notes_store.get_note(note_id)

    grounding_called = []

    async def fake_grounding(repo, question):
        grounding_called.append(repo)
        return [], True

    monkeypatch.setattr(pipeline.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(pipeline, "revalidate_note", fake_revalidate)
    monkeypatch.setattr(pipeline, "_code_grounding_for_repo", fake_grounding)
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope("answer"))])

    result = await pipeline.answer_question("q")
    assert grounding_called == []
    assert result["code_unavailable"] == []
    assert result["code_sources"] == []


# ---------------------------------------------------------------------------
# Personal/Behavior folder (Joseph, 2026-07-03): notes.PERSONAL_TAB_ID notes
# are behavioral context injected into every LLM call via _invoke_claude's
# single choke point — never embedded, never a search/ask source, never
# auto-structured.
# ---------------------------------------------------------------------------


def test_personal_context_empty_when_no_personal_notes(pipeline, notes_store):
    assert pipeline._personal_context() == ""


def test_personal_context_concatenates_raw_text(pipeline, notes_store):
    notes_store.add_note("Rule one.", tab_id=notes_store.PERSONAL_TAB_ID)
    notes_store.add_note("Rule two.", tab_id=notes_store.PERSONAL_TAB_ID)
    # A regular note must not leak into the personal context.
    notes_store.add_note("Not a behavioral rule.", tab_id="default")

    context = pipeline._personal_context()
    assert "Rule one." in context
    assert "Rule two." in context
    assert "Not a behavioral rule." not in context


def test_personal_context_bounded(pipeline, notes_store, monkeypatch):
    monkeypatch.setattr(pipeline, "_MAX_PERSONAL_CONTEXT_CHARS", 20)
    notes_store.add_note("x" * 100, tab_id=notes_store.PERSONAL_TAB_ID)
    assert len(pipeline._personal_context()) == 20


def test_prepend_personal_context_noop_when_empty(pipeline, notes_store):
    assert pipeline._prepend_personal_context("base instruction") == "base instruction"


def test_prepend_personal_context_prepends_when_present(pipeline, notes_store):
    notes_store.add_note("Always be terse.", tab_id=notes_store.PERSONAL_TAB_ID)
    result = pipeline._prepend_personal_context("base instruction")
    assert "Always be terse." in result
    assert result.endswith("base instruction")


def test_personal_context_sensitive_note_never_leaks_real_secret(pipeline, notes_store):
    """CRITICAL cross-note leak (2026-07-04): a sensitive note filed into
    the Personal folder must not inject its real secret into EVERY other
    LLM call via this concatenation — the redacted twin goes in instead."""
    secret = "sk-ant-" + "n" * 30
    notes_store.add_note(
        f"Remember this key: {secret}", tab_id=notes_store.PERSONAL_TAB_ID, sensitive=True
    )
    result = pipeline._personal_context()
    assert secret not in result


def test_personal_context_mixes_sensitive_and_normal_notes_correctly(pipeline, notes_store):
    secret = "sk-ant-" + "o" * 30
    notes_store.add_note("Always be terse.", tab_id=notes_store.PERSONAL_TAB_ID)
    notes_store.add_note(f"key: {secret}", tab_id=notes_store.PERSONAL_TAB_ID, sensitive=True)
    result = pipeline._personal_context()
    assert "Always be terse." in result  # normal note's real content still flows through
    assert secret not in result  # sensitive note's real secret does not


async def test_invoke_claude_includes_personal_context_in_system_prompt(
    pipeline, notes_store, monkeypatch
):
    """End-to-end through the real choke point — not just the helper
    functions in isolation."""
    notes_store.add_note("Write like a pirate.", tab_id=notes_store.PERSONAL_TAB_ID)
    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        return _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)
    await pipeline.transform_note("raw text")

    assert len(captured_args) == 1
    system_prompt_idx = captured_args[0].index("--append-system-prompt") + 1
    assert "Write like a pirate." in captured_args[0][system_prompt_idx]


def test_seed_personal_context_if_empty_seeds_once(pipeline, notes_store):
    seeded_first = pipeline.seed_personal_context_if_empty()
    assert seeded_first is True
    personal_notes = notes_store.list_notes(tab_id=notes_store.PERSONAL_TAB_ID)
    assert len(personal_notes) == 1
    assert personal_notes[0]["status"] == "processed"
    assert personal_notes[0]["pipeline"] is None
    assert personal_notes[0]["repo"] == notes_store.GENERAL_REPO

    seeded_second = pipeline.seed_personal_context_if_empty()
    assert seeded_second is False
    assert len(notes_store.list_notes(tab_id=notes_store.PERSONAL_TAB_ID)) == 1


def test_seed_personal_context_skipped_when_notes_already_present(pipeline, notes_store):
    notes_store.add_note("Existing rule.", tab_id=notes_store.PERSONAL_TAB_ID)
    assert pipeline.seed_personal_context_if_empty() is False
    assert len(notes_store.list_notes(tab_id=notes_store.PERSONAL_TAB_ID)) == 1


# ---------------------------------------------------------------------------
# Grimoire (2026-07-04): study guides — deterministic TOC parsing +
# trigger_study_guide_pipeline + schedule_pipeline's kind branch.
# ---------------------------------------------------------------------------

_GUIDE_PAYLOAD = {
    "abstract": "a guide about widgets",
    "tags": ["widgets"],
    "entities": ["widget.py"],
}


def test_parse_toc_basic_headings(pipeline):
    text = "# Title\n\nintro\n\n## Section One\n\ntext\n\n### Sub\n\nmore\n\n## Section Two\n"
    toc = pipeline._parse_toc(text)
    assert toc == [
        {"title": "Title", "anchor": "title", "level": 1},
        {"title": "Section One", "anchor": "section-one", "level": 2},
        {"title": "Sub", "anchor": "sub", "level": 3},
        {"title": "Section Two", "anchor": "section-two", "level": 2},
    ]


def test_parse_toc_empty_when_no_headings(pipeline):
    assert pipeline._parse_toc("just some\nplain paragraphs\nno headings at all") == []


def test_parse_toc_skips_fenced_code_blocks(pipeline):
    text = (
        "# Real Heading\n\n"
        "```python\n"
        "# this is a comment, not a heading\n"
        "def foo(): pass\n"
        "```\n\n"
        "## Another Real Heading\n"
    )
    toc = pipeline._parse_toc(text)
    assert [h["title"] for h in toc] == ["Real Heading", "Another Real Heading"]


def test_parse_toc_disambiguates_duplicate_titles(pipeline):
    text = "# Doc\n\n## Example\n\nfoo\n\n## Example\n\nbar\n"
    toc = pipeline._parse_toc(text)
    anchors = [h["anchor"] for h in toc]
    assert anchors == ["doc", "example", "example-1"]


def test_parse_toc_ignores_trailing_closing_hashes(pipeline):
    """ATX-style closing hashes (`## Title ##`) must not leak into the
    parsed title or the slug."""
    toc = pipeline._parse_toc("## My Heading ##\n")
    assert toc == [{"title": "My Heading", "anchor": "my-heading", "level": 2}]


def _stub_organize_after_structuring(monkeypatch):
    """Grimoire Phase 2's post-structuring organize hook is a SEPARATE
    concern (tested in its own right in test_notebook_organizer.py) — stub
    it to a no-op here so tests of the structuring pipeline itself don't
    also need to queue/mock an organize-pass LLM call."""
    from khimaira.monitor import notebook_organizer

    async def fake_organize_after_structuring(note_id):
        return None

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", fake_organize_after_structuring
    )


async def test_trigger_study_guide_pipeline_success_never_touches_raw_text(
    pipeline, notes_store, monkeypatch
):
    _stub_organize_after_structuring(monkeypatch)
    guide = notes_store.add_study_guide("# Widgets\n\n## Overview\n\nAll about widgets.")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_GUIDE_PAYLOAD)))])

    await pipeline.trigger_study_guide_pipeline(guide["id"])

    updated = notes_store.get_note(guide["id"])
    assert updated["status"] == "processed"
    assert updated["raw_text"] == "# Widgets\n\n## Overview\n\nAll about widgets."  # untouched
    assert updated["pipeline"]["abstract"] == "a guide about widgets"
    assert updated["pipeline"]["tags"] == ["widgets"]
    assert updated["pipeline"]["entities"] == ["widget.py"]
    assert updated["pipeline"]["toc"] == [
        {"title": "Widgets", "anchor": "widgets", "level": 1},
        {"title": "Overview", "anchor": "overview", "level": 2},
    ]
    # Guide title is never LLM-touched — stays whatever add_study_guide
    # derived from raw_text (here, the literal first line).
    assert updated["title"] == "# Widgets"


async def test_trigger_study_guide_pipeline_sensitive_guide_feeds_redacted_text(
    pipeline, notes_store, monkeypatch
):
    """Sensitive notes (2026-07-04): a sensitive guide's structuring
    subprocess receives llm_view(record) — the redacted twin — via stdin,
    never the real raw_text. _parse_toc (deterministic, local-only) still
    runs on the REAL raw_text — that's not an LLM egress."""
    _stub_organize_after_structuring(monkeypatch)
    secret = "sk-ant-" + "b" * 30
    guide = notes_store.add_study_guide(f"# Widgets\n\nAPI_KEY={secret}", sensitive=True)
    seen_stdin: list[str] = []

    class _CapturingProc:
        def __init__(self, stdout):
            self._stdout = stdout
            self.returncode = 0

        async def communicate(self, input=None):
            seen_stdin.append(input.decode("utf-8"))
            return self._stdout, b""

    async def fake_exec(*args, **kwargs):
        return _CapturingProc(_envelope(json.dumps(_GUIDE_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)

    await pipeline.trigger_study_guide_pipeline(guide["id"])

    assert len(seen_stdin) == 1
    assert secret not in seen_stdin[0]
    # toc still reflects the REAL document's headings (deterministic, never
    # sent to an LLM) — unaffected by redaction.
    updated = notes_store.get_note(guide["id"])
    assert updated["pipeline"]["toc"] == [{"title": "Widgets", "anchor": "widgets", "level": 1}]


async def test_trigger_study_guide_pipeline_failure_marks_failed(
    pipeline, notes_store, monkeypatch
):
    guide = notes_store.add_study_guide("# Widgets\n\nbody")
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("garbage")),
            _FakeProc(_envelope("still garbage")),
            _FakeProc(_envelope("still garbage too")),
        ],
    )

    await pipeline.trigger_study_guide_pipeline(guide["id"])

    updated = notes_store.get_note(guide["id"])
    assert updated["status"] == "failed"
    assert updated["raw_text"] == "# Widgets\n\nbody"
    assert updated["pipeline"] is None


async def test_trigger_study_guide_pipeline_calls_organize_after_structuring(
    pipeline, notes_store, monkeypatch
):
    """Grimoire Phase 2: a successful structuring pass fires the organizer's
    post-structuring hook (notebook_organizer.organize_after_structuring),
    scoped to exactly the guide that just finished — the placement logic
    itself is exercised in test_notebook_organizer.py, not here."""
    from khimaira.monitor import notebook_organizer

    guide = notes_store.add_study_guide("# Widgets\n\nbody")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_GUIDE_PAYLOAD)))])
    calls: list[str] = []

    async def fake_organize_after_structuring(note_id):
        calls.append(note_id)

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", fake_organize_after_structuring
    )

    await pipeline.trigger_study_guide_pipeline(guide["id"])

    assert calls == [guide["id"]]


async def test_trigger_study_guide_pipeline_skip_organize_suppresses_the_hook(
    pipeline, notes_store, monkeypatch
):
    """Grimoire chat-model addendum (2026-07-04): skip_organize=True (the
    chat auto-apply path) must structure normally but NOT fire the
    organize hook — a chatty edit sequence shouldn't fire N organize
    calls in a row."""
    from khimaira.monitor import notebook_organizer

    guide = notes_store.add_study_guide("# Widgets\n\nbody")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_GUIDE_PAYLOAD)))])
    calls: list[str] = []

    async def fake_organize_after_structuring(note_id):
        calls.append(note_id)

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", fake_organize_after_structuring
    )

    await pipeline.trigger_study_guide_pipeline(guide["id"], skip_organize=True)

    assert calls == []
    updated = notes_store.get_note(guide["id"])
    assert updated["status"] == "processed"  # structuring itself still ran


async def test_trigger_study_guide_pipeline_organize_failure_does_not_break_structuring(
    pipeline, notes_store, monkeypatch
):
    """organize_after_structuring already fails open internally (see its own
    docstring/tests) — this confirms trigger_study_guide_pipeline doesn't
    ALSO need its own guard: even if the hook somehow raised, the guide's
    structuring result (already written before the hook runs) must stand."""
    from khimaira.monitor import notebook_organizer

    guide = notes_store.add_study_guide("# Widgets\n\nbody")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_GUIDE_PAYLOAD)))])

    async def raising_organize_after_structuring(note_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        notebook_organizer, "organize_after_structuring", raising_organize_after_structuring
    )

    with pytest.raises(RuntimeError):
        await pipeline.trigger_study_guide_pipeline(guide["id"])

    # The structuring write already landed before the hook ran.
    updated = notes_store.get_note(guide["id"])
    assert updated["status"] == "processed"
    assert updated["pipeline"]["abstract"] == "a guide about widgets"


async def test_schedule_pipeline_dispatches_study_guide_to_the_guide_pipeline(
    pipeline, notes_store, monkeypatch
):
    guide = notes_store.add_study_guide("# G\n\nbody")
    called = {"guide": False, "note": False}

    async def fake_guide_pipeline(note_id, **kwargs):
        called["guide"] = True

    async def fake_note_pipeline(note_id, **kwargs):
        called["note"] = True

    monkeypatch.setattr(pipeline, "trigger_study_guide_pipeline", fake_guide_pipeline)
    monkeypatch.setattr(pipeline, "trigger_pipeline", fake_note_pipeline)

    pipeline.schedule_pipeline(guide["id"])
    await asyncio.sleep(0.05)  # let the fire-and-forget task run

    assert called == {"guide": True, "note": False}


async def test_schedule_pipeline_dispatches_regular_note_to_the_note_pipeline(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("raw")
    called = {"guide": False, "note": False}

    async def fake_guide_pipeline(note_id):
        called["guide"] = True

    async def fake_note_pipeline(note_id, **kwargs):
        called["note"] = True

    monkeypatch.setattr(pipeline, "trigger_study_guide_pipeline", fake_guide_pipeline)
    monkeypatch.setattr(pipeline, "trigger_pipeline", fake_note_pipeline)

    pipeline.schedule_pipeline(note["id"])
    await asyncio.sleep(0.05)

    assert called == {"guide": False, "note": True}


# ---------------------------------------------------------------------------
# Grimoire chat-model addendum — reprocess_after_raw_text_change: the shared
# helper the PATCH route and the chat auto-apply path both call after a
# raw_text write has already landed. Does NOT itself touch raw_text.
# ---------------------------------------------------------------------------


def test_reprocess_after_raw_text_change_flips_to_draft_and_schedules(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("raw")
    notes_store.update_note(note["id"], status="processed")
    scheduled: list[tuple] = []
    upserted: list[dict] = []

    monkeypatch.setattr(
        pipeline, "schedule_pipeline", lambda nid, **kw: scheduled.append((nid, kw))
    )
    monkeypatch.setattr(
        pipeline.notebook_retrieval, "schedule_upsert", lambda record: upserted.append(record)
    )

    updated = pipeline.reprocess_after_raw_text_change(note["id"])

    assert updated["status"] == "draft"
    assert scheduled == [(note["id"], {"skip_organize": False})]
    assert upserted == [updated]


def test_reprocess_after_raw_text_change_forwards_skip_organize(pipeline, notes_store, monkeypatch):
    note = notes_store.add_note("raw")
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        pipeline, "schedule_pipeline", lambda nid, **kw: scheduled.append((nid, kw))
    )
    monkeypatch.setattr(pipeline.notebook_retrieval, "schedule_upsert", lambda record: None)

    pipeline.reprocess_after_raw_text_change(note["id"], skip_organize=True)

    assert scheduled == [(note["id"], {"skip_organize": True})]


def test_reprocess_after_raw_text_change_skips_personal_tab_notes(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_note("x", tab_id=notes_store.PERSONAL_TAB_ID)
    notes_store.update_note(note["id"], status="processed")
    scheduled: list = []
    monkeypatch.setattr(pipeline, "schedule_pipeline", lambda nid, **kw: scheduled.append(nid))

    updated = pipeline.reprocess_after_raw_text_change(note["id"])

    assert scheduled == []
    assert updated["status"] == "processed"  # never flipped to draft


def test_reprocess_after_raw_text_change_unknown_id_raises(pipeline):
    with pytest.raises(ValueError, match="No note with id"):
        pipeline.reprocess_after_raw_text_change("no-such-note")


# ---------------------------------------------------------------------------
# Grimoire Phase 3 — the research-scientist toolbar. `claude -p`'s subprocess
# is mocked via canned stream-json stdout (one JSON object per line, mirroring
# `--output-format stream-json`'s real shape) for `_invoke_claude_agentic`
# itself; higher-level tests (`_invoke_agentic_grounded`, `research_answer`,
# `research_revise`) mock the layer below them directly, same convention as
# the rest of this file.
# ---------------------------------------------------------------------------


def _stream_line(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def _stream_stdout(*events: dict) -> bytes:
    return b"".join(_stream_line(e) for e in events)


def _assistant_tool_use(name: str, tool_input: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input or {}}]},
    }


def _result_event(result: str, total_cost_usd: float = 0.5) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": result,
        "total_cost_usd": total_cost_usd,
    }


async def test_invoke_claude_agentic_detects_web_tool_use(pipeline, monkeypatch):
    stdout = _stream_stdout(
        {"type": "system", "subtype": "init"},
        _assistant_tool_use("WebSearch", {"query": "x"}),
        _result_event('{"answer": "hi"}', total_cost_usd=0.42),
    )
    _queue_responses(pipeline, monkeypatch, [_FakeProc(stdout)])

    result = await pipeline._invoke_claude_agentic("question", "instruction")

    assert result["web_grounded"] is True
    assert result["result"] == '{"answer": "hi"}'
    assert result["total_cost_usd"] == 0.42


async def test_invoke_claude_agentic_webfetch_also_counts(pipeline, monkeypatch):
    stdout = _stream_stdout(
        _assistant_tool_use("WebFetch", {"url": "https://x"}),
        _result_event('{"answer": "hi"}'),
    )
    _queue_responses(pipeline, monkeypatch, [_FakeProc(stdout)])
    result = await pipeline._invoke_claude_agentic("q", "i")
    assert result["web_grounded"] is True


async def test_invoke_claude_agentic_no_web_tool_use_reports_ungrounded(pipeline, monkeypatch):
    stdout = _stream_stdout(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        _result_event('{"answer": "hi"}'),
    )
    _queue_responses(pipeline, monkeypatch, [_FakeProc(stdout)])

    result = await pipeline._invoke_claude_agentic("question", "instruction")
    assert result["web_grounded"] is False


async def test_invoke_claude_agentic_ignores_non_web_tool_use(pipeline, monkeypatch):
    stdout = _stream_stdout(
        _assistant_tool_use("Read", {"file_path": "/x"}),
        _result_event('{"answer": "hi"}'),
    )
    _queue_responses(pipeline, monkeypatch, [_FakeProc(stdout)])
    result = await pipeline._invoke_claude_agentic("q", "i")
    assert result["web_grounded"] is False


async def test_invoke_claude_agentic_nonzero_exit_raises(pipeline, monkeypatch):
    _queue_responses(pipeline, monkeypatch, [_FakeProc(b"", stderr=b"boom", returncode=1)])
    with pytest.raises(RuntimeError, match="exited 1"):
        await pipeline._invoke_claude_agentic("q", "i")


async def test_invoke_claude_agentic_missing_result_raises(pipeline, monkeypatch):
    stdout = _stream_stdout({"type": "system", "subtype": "init"})
    _queue_responses(pipeline, monkeypatch, [_FakeProc(stdout)])
    with pytest.raises(ValueError, match="no final result"):
        await pipeline._invoke_claude_agentic("q", "i")


async def test_invoke_claude_agentic_passes_allowed_tools_and_add_dir(
    pipeline, monkeypatch, tmp_path
):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(_stream_stdout(_result_event('{"answer":"x"}')))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)
    await pipeline._invoke_claude_agentic("q", "i", repo_root=tmp_path, max_budget_usd=1.0)

    args = captured["args"]
    assert args[args.index("--allowedTools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert args[args.index("--add-dir") + 1] == str(tmp_path)
    assert args[args.index("--max-budget-usd") + 1] == "1.0"
    assert args[args.index("--output-format") + 1] == "stream-json"


# --- per-call-unique CLAUDE_CONFIG_DIR (Grimoire Phase 4 addendum) ---


def test_isolated_config_dir_is_unique_per_call(pipeline):
    first = pipeline._isolated_config_dir()
    second = pipeline._isolated_config_dir()
    try:
        assert first != second
        assert first.is_dir()
        assert second.is_dir()
    finally:
        pipeline._cleanup_config_dir(first)
        pipeline._cleanup_config_dir(second)


def test_isolated_config_dir_writes_settings_and_no_credentials_when_missing(pipeline, monkeypatch):
    real_expanduser = os.path.expanduser

    def fake_expanduser(path):
        if path == "~/.claude/.credentials.json":
            return "/nonexistent/path/never/here"
        return real_expanduser(path)

    monkeypatch.setattr(pipeline.os.path, "expanduser", fake_expanduser)
    cfg_dir = pipeline._isolated_config_dir()
    try:
        assert (cfg_dir / "settings.json").read_text() == "{}"
        assert not (cfg_dir / ".credentials.json").exists()
    finally:
        pipeline._cleanup_config_dir(cfg_dir)


def test_cleanup_config_dir_removes_the_directory(pipeline):
    cfg_dir = pipeline._isolated_config_dir()
    assert cfg_dir.exists()
    pipeline._cleanup_config_dir(cfg_dir)
    assert not cfg_dir.exists()


def test_cleanup_config_dir_is_fail_open_on_missing_dir(pipeline, tmp_path):
    already_gone = tmp_path / "never-existed"
    pipeline._cleanup_config_dir(already_gone)  # must not raise


async def test_invoke_claude_cleans_up_config_dir_after_success(pipeline, monkeypatch):
    seen_cfg_dirs: list = []

    async def fake_exec(*args, **kwargs):
        seen_cfg_dirs.append(pipeline.Path(kwargs["env"]["CLAUDE_CONFIG_DIR"]))
        return _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)
    await pipeline._invoke_claude("content", "instruction")

    assert len(seen_cfg_dirs) == 1
    assert not seen_cfg_dirs[0].exists()  # cleaned up after the call


async def test_invoke_claude_cleans_up_config_dir_even_on_failure(pipeline, monkeypatch):
    seen_cfg_dirs: list = []

    async def fake_exec(*args, **kwargs):
        seen_cfg_dirs.append(pipeline.Path(kwargs["env"]["CLAUDE_CONFIG_DIR"]))
        return _FakeProc(b"", stderr=b"boom", returncode=1)

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError):
        await pipeline._invoke_claude("content", "instruction")

    assert not seen_cfg_dirs[0].exists()  # cleaned up despite the raise


async def test_invoke_claude_concurrent_calls_use_different_config_dirs(pipeline, monkeypatch):
    """The actual property this whole fix exists for: two GENUINELY
    concurrent calls must never share a CLAUDE_CONFIG_DIR."""
    seen_cfg_dirs: list = []
    lock = asyncio.Lock()

    async def fake_exec(*args, **kwargs):
        async with lock:
            seen_cfg_dirs.append(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        await asyncio.sleep(0.02)
        return _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)
    await asyncio.gather(
        pipeline._invoke_claude("c1", "i"),
        pipeline._invoke_claude("c2", "i"),
        pipeline._invoke_claude("c3", "i"),
    )

    assert len(set(seen_cfg_dirs)) == 3


# --- _invoke_agentic_grounded: the retry-on-unverified-grounding contract ---

_GROUNDED_TRUE = {
    "answer": "hi",
    "code_citations": [],
    "web_citations": ["http://x"],
    "proposed_patch": None,
}
_GROUNDED_EMPTY = {
    "answer": "hi",
    "code_citations": [],
    "web_citations": [],
    "proposed_patch": None,
}


async def test_invoke_agentic_grounded_returns_parsed_result_when_grounded(pipeline, monkeypatch):
    async def fake_invoke(content, instruction, *, repo_root, max_budget_usd):
        return {"result": json.dumps(_GROUNDED_TRUE), "web_grounded": True, "total_cost_usd": 0.5}

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", fake_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert result["answer"] == "hi"
    assert result["web_grounded"] is True
    assert result["web_grounding_unverified"] is False
    assert result["total_cost_usd"] == 0.5


async def test_invoke_agentic_grounded_retries_once_when_citations_but_ungrounded(
    pipeline, monkeypatch
):
    calls: list[str] = []

    async def fake_invoke(content, instruction, *, repo_root, max_budget_usd):
        calls.append(instruction)
        if len(calls) == 1:
            return {
                "result": json.dumps(_GROUNDED_TRUE),
                "web_grounded": False,
                "total_cost_usd": 0.3,
            }
        payload = {**_GROUNDED_TRUE, "answer": "hi2"}
        return {"result": json.dumps(payload), "web_grounded": True, "total_cost_usd": 0.6}

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", fake_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert len(calls) == 2
    assert "STRICT" in calls[1]
    assert result["answer"] == "hi2"
    assert result["web_grounding_unverified"] is False


async def test_invoke_agentic_grounded_flags_unverified_when_retry_also_fails(
    pipeline, monkeypatch
):
    calls: list[str] = []

    async def fake_invoke(content, instruction, *, repo_root, max_budget_usd):
        calls.append(instruction)
        return {"result": json.dumps(_GROUNDED_TRUE), "web_grounded": False, "total_cost_usd": 0.3}

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", fake_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert len(calls) == 2  # one retry, then gives up
    assert result["web_grounding_unverified"] is True


async def test_invoke_agentic_grounded_no_retry_when_no_citations_claimed(pipeline, monkeypatch):
    calls: list[str] = []

    async def fake_invoke(content, instruction, *, repo_root, max_budget_usd):
        calls.append(instruction)
        return {"result": json.dumps(_GROUNDED_EMPTY), "web_grounded": False, "total_cost_usd": 0.1}

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", fake_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert len(calls) == 1  # nothing to distrust — no citations claimed
    assert result["web_grounding_unverified"] is False


async def test_invoke_agentic_grounded_handles_invocation_error(pipeline, monkeypatch):
    async def failing_invoke(content, instruction, *, repo_root, max_budget_usd):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", failing_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert result["web_grounding_unverified"] is True
    assert "failed" in result["answer"].lower()


async def test_invoke_agentic_grounded_handles_unparseable_response(pipeline, monkeypatch):
    async def fake_invoke(content, instruction, *, repo_root, max_budget_usd):
        return {"result": "not json", "web_grounded": True, "total_cost_usd": 0.1}

    monkeypatch.setattr(pipeline, "_invoke_claude_agentic", fake_invoke)
    result = await pipeline._invoke_agentic_grounded("q", "i", repo_root=None, max_budget_usd=1.0)

    assert result["web_grounding_unverified"] is True


# --- research_answer (ANSWER path) ---


async def test_research_answer_unknown_note_raises(pipeline):
    with pytest.raises(ValueError, match="No note with id"):
        await pipeline.research_answer("no-such-note", "question")


async def test_research_answer_general_repo_skips_repo_root(pipeline, notes_store, monkeypatch):
    note = notes_store.add_study_guide("# G\n\nbody", repo=notes_store.GENERAL_REPO)
    seen: list = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        seen.append(repo_root)
        return {
            **_GROUNDED_EMPTY,
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.1,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    await pipeline.research_answer(note["id"], "question")

    assert seen == [None]


async def test_research_answer_sensitive_guide_feeds_redacted_text(
    pipeline, notes_store, monkeypatch
):
    """Sensitive notes (2026-07-04): research_answer's agentic instruction
    must carry llm_view(record), never the real raw_text."""
    secret = "sk-ant-" + "k" * 30
    guide = notes_store.add_study_guide(f"# G\n\nAPI_KEY={secret}", sensitive=True)
    seen_instructions: list[str] = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        seen_instructions.append(instruction)
        return {**_GROUNDED_EMPTY, "web_grounding_unverified": False, "total_cost_usd": 0.1}

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    await pipeline.research_answer(guide["id"], "what does this do?")

    assert secret not in seen_instructions[0]


async def test_research_answer_resolves_repo_root_and_passes_question(
    pipeline, notes_store, monkeypatch, git_repo
):
    _patch_repo_root(pipeline, monkeypatch, "testrepo", git_repo)
    note = notes_store.add_study_guide("# G\n\nbody", repo="testrepo")
    seen: list = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        seen.append((content, repo_root))
        return {
            **_GROUNDED_EMPTY,
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.1,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    await pipeline.research_answer(note["id"], "my question")

    assert seen[0] == ("my question", git_repo)


# --- splice_section (deterministic REVISE apply helper) ---


def test_splice_section_replaces_middle_section(pipeline):
    raw = "# Title\n\nintro\n\n## A\n\nold a\n\n## B\n\nold b\n"
    result = pipeline.splice_section(raw, "a", "## A\n\nNEW A\n")
    assert "NEW A" in result
    assert "old a" not in result
    assert "old b" in result
    assert "intro" in result


def test_splice_section_replaces_last_section(pipeline):
    raw = "# Title\n\n## A\n\na body\n\n## B\n\nb body\n"
    result = pipeline.splice_section(raw, "b", "## B\n\nNEW B\n")
    assert "NEW B" in result
    assert "b body" not in result
    assert "## A" in result
    assert "a body" in result


def test_splice_section_consumes_nested_subsections_of_the_replaced_section(pipeline):
    raw = "# Title\n\n## A\n\n### Sub\n\nsub text\n\n## B\n\nb text\n"
    result = pipeline.splice_section(raw, "a", "## A\n\nNEW A ONLY\n")
    assert "NEW A ONLY" in result
    assert "sub text" not in result
    assert "## B" in result
    assert "b text" in result


def test_splice_section_unknown_anchor_raises(pipeline):
    raw = "# Title\n\n## A\n\nbody\n"
    with pytest.raises(ValueError, match="No section anchored"):
        pipeline.splice_section(raw, "nonexistent", "new")


def test_splice_section_skips_fenced_code_blocks(pipeline):
    raw = "# Title\n\n## A\n\n```\n# not a heading\n```\n\nbody\n\n## B\n\nb\n"
    result = pipeline.splice_section(raw, "a", "## A\n\nreplaced\n")
    assert "replaced" in result
    assert "not a heading" not in result
    assert "## B" in result


# --- research_revise (REVISE path) ---


async def test_research_revise_unknown_note_raises(pipeline):
    with pytest.raises(ValueError, match="No note with id"):
        await pipeline.research_revise("no-such-note", "directive")


async def test_research_revise_unknown_section_anchor_raises_before_invoking(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# Title\n\n## A\n\nbody\n")
    called = []

    async def fake_grounded(*args, **kwargs):
        called.append(1)
        return {}

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    with pytest.raises(ValueError, match="No section anchored"):
        await pipeline.research_revise(note["id"], "directive", section_anchor="nonexistent")

    assert called == []  # never spent the agentic call on a bad anchor


async def test_research_revise_sensitive_guide_feeds_redacted_text_but_splices_real(
    pipeline, notes_store, monkeypatch
):
    """Sensitive notes (2026-07-04): the agentic instruction carries
    llm_view(record) (redacted), but splice_section still operates on the
    REAL raw_text — REVISE's human-review-before-apply gate is what keeps
    that safe from a data-loss standpoint, per the audit."""
    secret = "sk-ant-" + "m" * 30
    raw = f"# Title\n\nAPI_KEY={secret}\n"
    guide = notes_store.add_study_guide(raw, sensitive=True)
    seen_instructions: list[str] = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        seen_instructions.append(instruction)
        return {
            "answer": "rewrote it",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": "# Title\n\nreplacement text\n",
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.2,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await pipeline.research_revise(guide["id"], "make it better")

    assert secret not in seen_instructions[0]
    assert result["proposed_raw_text"] == "# Title\n\nreplacement text\n"


async def test_research_revise_whole_guide_splices_full_replacement(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# Title\n\nold body\n")

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        assert "WHOLE guide" in instruction
        return {
            "answer": "rewrote it",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": "# Title\n\nNEW body\n",
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.2,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await pipeline.research_revise(note["id"], "make it better")

    assert result["proposed_raw_text"] == "# Title\n\nNEW body\n"
    # REVISE never applies — the note's own raw_text is untouched.
    assert notes_store.get_note(note["id"])["raw_text"] == "# Title\n\nold body\n"


async def test_research_revise_section_scoped_splices_into_original(
    pipeline, notes_store, monkeypatch
):
    raw = "# Title\n\n## A\n\nold a\n\n## B\n\nold b\n"
    note = notes_store.add_study_guide(raw)

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        assert "ONE section" in instruction
        return {
            "answer": "revised A",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": "## A\n\nNEW A\n",
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.2,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await pipeline.research_revise(note["id"], "improve section A", section_anchor="a")

    assert "NEW A" in result["proposed_raw_text"]
    assert "old b" in result["proposed_raw_text"]
    assert notes_store.get_note(note["id"])["raw_text"] == raw  # original untouched


async def test_research_revise_no_proposed_patch_leaves_proposed_raw_text_none(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# Title\n\nbody\n")

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd):
        return {
            "answer": "found nothing to change",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": None,
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.1,
        }

    monkeypatch.setattr(pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await pipeline.research_revise(note["id"], "directive")

    assert result["proposed_raw_text"] is None


# ---------------------------------------------------------------------------
# Grimoire Phase 4 addendum — research jobs (async, polled) instead of an
# await-in-request. research_answer/research_revise themselves are already
# covered above; these tests exercise ONLY the job scheduling/store/poll
# layer, mocking research_answer/research_revise directly.
# ---------------------------------------------------------------------------


async def test_schedule_research_answer_unknown_note_raises_before_scheduling(
    pipeline, monkeypatch
):
    called = []

    async def fake_answer(note_id, question, *, max_budget_usd):
        called.append(1)

    monkeypatch.setattr(pipeline, "research_answer", fake_answer)
    with pytest.raises(ValueError, match="No note with id"):
        pipeline.schedule_research_answer("no-such-note", "question")
    await asyncio.sleep(0.05)
    assert called == []  # never scheduled a background task on a bad note_id


async def test_schedule_research_answer_job_completes_and_is_pollable(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# G\n\nbody")

    async def fake_answer(note_id, question, *, max_budget_usd):
        assert note_id == note["id"]
        assert question == "q"
        return {
            "answer": "hi",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": None,
            "web_grounded": True,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.3,
        }

    monkeypatch.setattr(pipeline, "research_answer", fake_answer)
    job_id = pipeline.schedule_research_answer(note["id"], "q")

    assert pipeline.get_research_job(job_id)["status"] == "pending"
    await asyncio.sleep(0.05)  # let the background task run

    job = pipeline.get_research_job(job_id)
    assert job["status"] == "done"
    assert job["kind"] == "answer"
    assert job["answer"] == "hi"


async def test_schedule_research_answer_job_reports_error_on_exception(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# G\n\nbody")

    async def failing_answer(note_id, question, *, max_budget_usd):
        raise RuntimeError("agentic call blew up")

    monkeypatch.setattr(pipeline, "research_answer", failing_answer)
    job_id = pipeline.schedule_research_answer(note["id"], "q")
    await asyncio.sleep(0.05)

    job = pipeline.get_research_job(job_id)
    assert job["status"] == "error"
    assert job["kind"] == "answer"
    assert "agentic call blew up" in job["error"]


async def test_schedule_research_revise_unknown_note_raises_before_scheduling(pipeline):
    with pytest.raises(ValueError, match="No note with id"):
        pipeline.schedule_research_revise("no-such-note", "directive")


async def test_schedule_research_revise_unknown_section_anchor_raises_before_scheduling(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# Title\n\n## A\n\nbody")
    called = []

    async def fake_revise(note_id, directive, *, section_anchor, max_budget_usd):
        called.append(1)

    monkeypatch.setattr(pipeline, "research_revise", fake_revise)
    with pytest.raises(ValueError, match="No section anchored"):
        pipeline.schedule_research_revise(note["id"], "directive", section_anchor="nonexistent")
    await asyncio.sleep(0.05)
    assert called == []


async def test_schedule_research_revise_job_completes_and_is_pollable(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# G\n\nbody")

    async def fake_revise(note_id, directive, *, section_anchor, max_budget_usd):
        return {
            "answer": "changed it",
            "code_citations": [],
            "web_citations": [],
            "proposed_patch": "new text",
            "proposed_raw_text": "# G\n\nnew text",
            "web_grounded": False,
            "web_grounding_unverified": False,
            "total_cost_usd": 0.2,
        }

    monkeypatch.setattr(pipeline, "research_revise", fake_revise)
    job_id = pipeline.schedule_research_revise(note["id"], "improve it")
    await asyncio.sleep(0.05)

    job = pipeline.get_research_job(job_id)
    assert job["status"] == "done"
    assert job["kind"] == "revise"
    assert job["proposed_raw_text"] == "# G\n\nnew text"


async def test_schedule_research_revise_job_reports_error_on_exception(
    pipeline, notes_store, monkeypatch
):
    note = notes_store.add_study_guide("# G\n\nbody")

    async def failing_revise(note_id, directive, *, section_anchor, max_budget_usd):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "research_revise", failing_revise)
    job_id = pipeline.schedule_research_revise(note["id"], "improve it")
    await asyncio.sleep(0.05)

    job = pipeline.get_research_job(job_id)
    assert job["status"] == "error"
    assert job["kind"] == "revise"
    assert "boom" in job["error"]


def test_get_research_job_unknown_id_raises(pipeline):
    with pytest.raises(ValueError, match="No research job with id"):
        pipeline.get_research_job("no-such-job")
