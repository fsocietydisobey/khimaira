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
    monkeypatch.setattr(pipeline_mod, "_seance_code_search", lambda repo, question: ([], False, False))
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
                    json.dumps({**_ANCHORED_PAYLOAD, "title": "Fresh backfilled title", "unchanged": True})
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
