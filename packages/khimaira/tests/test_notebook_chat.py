"""Tests for khimaira.monitor.notebook_chat (Grimoire chat-model backend).

`claude -p`'s subprocess is mocked at the `_invoke_agentic_grounded`/
`_invoke_claude` layer (same convention as test_notebook_pipeline.py) — these
tests exercise the chat-specific orchestration (history storage, answer-vs-
edit routing, edit validation/apply, compact/clear), not the real CLI.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


@pytest.fixture
def chat(notes_store, monkeypatch):
    from khimaira.monitor import notebook_chat as chat_mod
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    importlib.reload(pipeline_mod)
    importlib.reload(chat_mod)
    # Default the related-notes retrieval (2026-07-06) to "no hits" — most
    # tests in this file aren't exercising that feature, and without this
    # every run_chat_turn call would hit a real embeddings/Qdrant backend
    # (same leaf-defaulting rationale as test_notebook_pipeline.py's
    # `pipeline` fixture patching _seance_code_search). Tests that DO
    # exercise related-notes injection override this per-test.
    async def _fake_search_notes_async(*args, **kwargs):
        return []

    monkeypatch.setattr(chat_mod.notebook_retrieval, "search_notes_async", _fake_search_notes_async)
    yield chat_mod


# ---------------------------------------------------------------------------
# Storage — one JSON sidecar per guide
# ---------------------------------------------------------------------------


def test_get_chat_history_empty_when_no_sidecar_yet(chat):
    assert chat.get_chat_history("no-such-note") == []


def test_append_chat_messages_round_trip(chat):
    chat.append_chat_messages("note-1", {"role": "user", "content": "hi", "ts": "t1"})
    chat.append_chat_messages("note-1", {"role": "assistant", "content": "hello", "ts": "t2"})

    history = chat.get_chat_history("note-1")
    assert [m["content"] for m in history] == ["hi", "hello"]


def test_append_chat_messages_does_not_leak_across_notes(chat):
    chat.append_chat_messages("note-1", {"role": "user", "content": "a", "ts": "t"})
    chat.append_chat_messages("note-2", {"role": "user", "content": "b", "ts": "t"})

    assert [m["content"] for m in chat.get_chat_history("note-1")] == ["a"]
    assert [m["content"] for m in chat.get_chat_history("note-2")] == ["b"]


def test_get_chat_history_fails_open_on_corrupt_sidecar(chat, monkeypatch):
    path = chat._chat_path("note-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert chat.get_chat_history("note-1") == []


def test_clear_chat_wipes_history(chat, notes_store):
    guide = notes_store.add_study_guide("# G\n\nbody")
    chat.append_chat_messages(guide["id"], {"role": "user", "content": "x", "ts": "t"})

    result = chat.clear_chat(guide["id"])

    assert result == {"cleared": True}
    assert chat.get_chat_history(guide["id"]) == []


def test_clear_chat_unknown_note_raises(chat):
    with pytest.raises(ValueError, match="No note with id"):
        chat.clear_chat("no-such-note")


# ---------------------------------------------------------------------------
# _format_chat_history_for_prompt
# ---------------------------------------------------------------------------


def test_format_chat_history_empty(chat):
    assert chat._format_chat_history_for_prompt([]) == "(no prior messages)"


def test_format_chat_history_renders_roles_and_edits(chat):
    history = [
        {"role": "user", "content": "what does this do?", "ts": "t1"},
        {"role": "assistant", "content": "it does X.", "ts": "t2", "edit": None},
        {"role": "user", "content": "update section A", "ts": "t3"},
        {
            "role": "assistant",
            "content": "done.",
            "ts": "t4",
            "edit": {"section_anchor": "a", "diff": "...", "applied_at": "t4"},
        },
    ]
    rendered = chat._format_chat_history_for_prompt(history)
    assert "User: what does this do?" in rendered
    assert "Assistant: it does X." in rendered
    assert "[applied an edit to a]" in rendered


def test_format_chat_history_whole_guide_edit_scope(chat):
    history = [
        {
            "role": "assistant",
            "content": "done.",
            "ts": "t",
            "edit": {"section_anchor": None, "diff": "...", "applied_at": "t"},
        }
    ]
    rendered = chat._format_chat_history_for_prompt(history)
    assert "[applied an edit to (whole guide)]" in rendered


# ---------------------------------------------------------------------------
# _try_apply_edit
# ---------------------------------------------------------------------------


async def test_try_apply_edit_whole_guide(chat, notes_store):
    """async: _try_apply_edit indirectly calls
    reprocess_after_raw_text_change -> schedule_pipeline ->
    asyncio.create_task, which needs a running event loop."""
    guide = notes_store.add_study_guide("# Title\n\nold body\n")
    edit = chat._try_apply_edit(
        guide["id"],
        "# Title\n\nold body\n",
        {"section_anchor": None, "new_text": "# Title\n\nnew body\n"},
    )

    assert edit is not None
    assert edit["section_anchor"] is None
    assert "new body" in edit["diff"]
    assert notes_store.get_note(guide["id"])["raw_text"] == "# Title\n\nnew body\n"


async def test_try_apply_edit_section_scoped_splices(chat, notes_store):
    raw = "# Title\n\n## A\n\nold a\n\n## B\n\nold b\n"
    guide = notes_store.add_study_guide(raw)

    edit = chat._try_apply_edit(
        guide["id"], raw, {"section_anchor": "a", "new_text": "## A\n\nnew a\n"}
    )

    assert edit["section_anchor"] == "a"
    updated_raw = notes_store.get_note(guide["id"])["raw_text"]
    assert "new a" in updated_raw
    assert "old b" in updated_raw  # sibling untouched


def test_try_apply_edit_empty_new_text_skips(chat, notes_store):
    raw = "# Title\n\nbody\n"
    guide = notes_store.add_study_guide(raw)

    edit = chat._try_apply_edit(guide["id"], raw, {"section_anchor": None, "new_text": "   "})

    assert edit is None
    assert notes_store.get_note(guide["id"])["raw_text"] == raw


def test_try_apply_edit_unknown_section_anchor_skips(chat, notes_store):
    raw = "# Title\n\n## A\n\nbody\n"
    guide = notes_store.add_study_guide(raw)

    edit = chat._try_apply_edit(
        guide["id"], raw, {"section_anchor": "nonexistent", "new_text": "## X\n\nnew\n"}
    )

    assert edit is None
    assert notes_store.get_note(guide["id"])["raw_text"] == raw


def test_try_apply_edit_reprocesses_with_skip_organize(chat, notes_store, monkeypatch):
    from khimaira.monitor import notebook_pipeline

    raw = "# Title\n\nold\n"
    guide = notes_store.add_study_guide(raw)
    seen: list = []
    monkeypatch.setattr(
        notebook_pipeline,
        "reprocess_after_raw_text_change",
        lambda nid, **kw: seen.append((nid, kw)),
    )

    chat._try_apply_edit(guide["id"], raw, {"section_anchor": None, "new_text": "# Title\n\nnew\n"})

    assert seen == [(guide["id"], {"skip_organize": True})]


# ---------------------------------------------------------------------------
# run_chat_turn — answer-vs-edit routing
# ---------------------------------------------------------------------------


def _grounded(answer="hi", edit=None, web_grounded=False, code_citations=None, web_citations=None):
    return {
        "answer": answer,
        "code_citations": code_citations or [],
        "web_citations": web_citations or [],
        "edit": edit,
        "web_grounded": web_grounded,
        "web_grounding_unverified": False,
        "total_cost_usd": 0.3,
    }


async def test_run_chat_turn_works_on_regular_notes(chat, notes_store, monkeypatch):
    """CHAT-UNIFY (2026-07-04): chat is no longer guide-only — a regular
    note is a valid chat target too, grounded in its own content + repo."""
    note = notes_store.add_note("just a note", repo="khimaira")
    captured = {}

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        captured["instruction"] = instruction
        return _grounded(answer="It's just a note.")

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(note["id"], "what is this?")

    assert result["message"]["content"] == "It's just a note."
    assert "just a note" in captured["instruction"]


async def test_run_chat_turn_unknown_note_raises(chat):
    with pytest.raises(ValueError, match="No note with id"):
        await chat.run_chat_turn("no-such-note", "hello")


async def test_run_chat_turn_answer_only_does_not_touch_raw_text(chat, notes_store, monkeypatch):
    raw = "# G\n\nbody\n"
    guide = notes_store.add_study_guide(raw)

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        assert content == "what is this?"
        assert schema is chat.ChatTurnOutput
        return _grounded(answer="It's a guide about X.")

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(guide["id"], "what is this?")

    assert result["message"]["content"] == "It's a guide about X."
    assert result["message"]["edit"] is None
    assert notes_store.get_note(guide["id"])["raw_text"] == raw

    history = chat.get_chat_history(guide["id"])
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "what is this?"
    assert history[1]["edit"] is None


async def test_run_chat_turn_threads_guide_own_repo_as_target_repo(chat, notes_store, monkeypatch):
    """Personal-context repo-scoping (2026-07-04,
    tasks/grimoire/PERSONAL-CONTEXT-SCOPING.md item 4): chat's target_repo
    must be the GUIDE's own repo, not hardcoded/omitted."""
    guide = notes_store.add_study_guide("# G\n\nbody\n", repo="jeevy_portal")
    captured = {}

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        captured["target_repo"] = kwargs.get("target_repo")
        return _grounded()

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    await chat.run_chat_turn(guide["id"], "hello")
    assert captured["target_repo"] == "jeevy_portal"


async def test_run_chat_turn_edit_auto_applies_and_records_diff(chat, notes_store, monkeypatch):
    raw = "# G\n\nold body\n"
    guide = notes_store.add_study_guide(raw)

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(
            answer="Updated it.", edit={"section_anchor": None, "new_text": "# G\n\nnew body\n"}
        )

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(guide["id"], "update the guide")

    assert result["message"]["edit"]["section_anchor"] is None
    assert "new body" in result["message"]["edit"]["diff"]
    assert notes_store.get_note(guide["id"])["raw_text"] == "# G\n\nnew body\n"

    history = chat.get_chat_history(guide["id"])
    assert history[1]["edit"]["section_anchor"] is None


async def test_run_chat_turn_sensitive_note_suppresses_proposed_edit(
    chat, notes_store, monkeypatch
):
    """Sensitive notes (2026-07-04): even if the model IGNORES the
    _SENSITIVE_CHAT_ADDENDUM and proposes an edit anyway, it must never be
    applied — auto-apply has no human gate, and the model only ever saw a
    redacted copy of a sensitive guide."""
    secret = "sk-ant-" + "q" * 30
    raw = f"# G\n\nAPI_KEY={secret}\n"
    guide = notes_store.add_study_guide(raw, sensitive=True)

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(
            answer="Updated it (but shouldn't have).",
            edit={"section_anchor": None, "new_text": "# G\n\nreplaced\n"},
        )

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(guide["id"], "update the key")

    assert result["message"]["edit"] is None  # suppressed, not applied
    assert notes_store.get_note(guide["id"])["raw_text"] == raw  # untouched


async def test_run_chat_turn_sensitive_note_instruction_carries_addendum_and_redacted_guide(
    chat, notes_store, monkeypatch
):
    secret = "sk-ant-" + "r" * 30
    guide = notes_store.add_study_guide(f"# G\n\nAPI_KEY={secret}\n", sensitive=True)
    seen_instructions: list[str] = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        seen_instructions.append(instruction)
        return _grounded(answer="hi")

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    await chat.run_chat_turn(guide["id"], "what's in here?")

    assert secret not in seen_instructions[0]
    assert "SENSITIVE" in seen_instructions[0]
    assert "MUST NOT propose an edit" in seen_instructions[0]


async def test_run_chat_turn_sensitive_regular_note_answer_only_and_redacted(
    chat, notes_store, monkeypatch
):
    """CHAT-UNIFY (2026-07-04): the sensitive-note answer-only + redaction
    guard must hold on the NOTE path too, not just guides — verified via a
    real add_note(sensitive=True) record, not inferred from code reading
    alone (record.get("sensitive") is kind-agnostic, but audit beats
    inspection — see bug-class-enumeration.md)."""
    secret = "sk-ant-" + "s" * 30
    raw = f"API_KEY={secret}"
    note = notes_store.add_note(raw, sensitive=True)
    seen_instructions: list[str] = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        seen_instructions.append(instruction)
        return _grounded(
            answer="Updated it (but shouldn't have).",
            edit={"section_anchor": None, "new_text": "replaced"},
        )

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await chat.run_chat_turn(note["id"], "what's the key?")

    assert secret not in seen_instructions[0]
    assert "SENSITIVE" in seen_instructions[0]
    assert result["message"]["edit"] is None  # suppressed, not applied
    assert notes_store.get_note(note["id"])["raw_text"] == raw  # untouched


async def test_run_chat_turn_non_sensitive_note_no_addendum(chat, notes_store, monkeypatch):
    guide = notes_store.add_study_guide("# G\n\nbody\n")
    seen_instructions: list[str] = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        seen_instructions.append(instruction)
        return _grounded(answer="hi")

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    await chat.run_chat_turn(guide["id"], "what's in here?")

    assert "SENSITIVE" not in seen_instructions[0]


async def test_run_chat_turn_bad_edit_falls_back_to_answer_only(chat, notes_store, monkeypatch):
    raw = "# G\n\nbody\n"
    guide = notes_store.add_study_guide(raw)

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(
            answer="Tried to update.",
            edit={"section_anchor": "nonexistent", "new_text": "whatever"},
        )

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(guide["id"], "update section X")

    assert result["message"]["edit"] is None
    assert notes_store.get_note(guide["id"])["raw_text"] == raw


async def test_run_chat_turn_passes_history_and_guide_into_instruction(
    chat, notes_store, monkeypatch
):
    guide = notes_store.add_study_guide("# G\n\nbody\n")
    chat.append_chat_messages(
        guide["id"], {"role": "user", "content": "earlier question", "ts": "t"}
    )
    seen_instructions = []

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        seen_instructions.append(instruction)
        return _grounded()

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    await chat.run_chat_turn(guide["id"], "a new question")

    assert "earlier question" in seen_instructions[0]
    assert "body" in seen_instructions[0]  # the guide's raw_text


async def test_run_chat_turn_returns_grounding_shape(chat, notes_store, monkeypatch):
    guide = notes_store.add_study_guide("# G\n\nbody\n")

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(web_grounded=True, code_citations=["a.py:1"], web_citations=["http://x"])

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await chat.run_chat_turn(guide["id"], "q")

    assert result["grounding"]["web_grounded"] is True
    assert result["grounding"]["code_citations"] == ["a.py:1"]
    assert result["grounding"]["web_citations"] == ["http://x"]
    assert result["total_cost_usd"] == 0.3


# ---------------------------------------------------------------------------
# Related-notes retrieval-injection (2026-07-06, Joseph bug report): the
# per-record chat subprocess has no live tool to browse other notes
# (deliberately MCP-free) — without this, it's architecturally blind to
# everything but the one document it's scoped to. Fix reuses the SAME
# semantic search answer_question already runs and injects the top matches
# as prompt context, reporting which notes were included via `sources`
# (same field/shape answer_question already returns).
# ---------------------------------------------------------------------------


async def test_run_chat_turn_injects_related_notes_into_instruction(chat, notes_store, monkeypatch):
    other = notes_store.add_note("the OTHER note's own body text", repo="khimaira")
    primary = notes_store.add_note("the primary note being chatted about", repo="khimaira")
    captured = {}

    async def fake_search(query, **kwargs):
        return [{"note_id": other["id"], "score": 0.9}]

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        captured["instruction"] = instruction
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(primary["id"], "what does the other note say?")

    assert "the OTHER note's own body text" in captured["instruction"]
    assert result["sources"] == [other["id"]]
    # Persisted history must carry sources too — the frontend refetches
    # history rather than trusting the job's transient result, so this is
    # what actually renders in the UI.
    history = chat.get_chat_history(primary["id"])
    assert history[1]["sources"] == [other["id"]]


async def test_run_chat_turn_excludes_self_from_related_notes(chat, notes_store, monkeypatch):
    """Semantic search can legitimately return the primary note itself as a
    top hit — it must never show up in its own 'other notes' section."""
    primary = notes_store.add_note("primary note body", repo="khimaira")

    async def fake_search(query, **kwargs):
        return [{"note_id": primary["id"], "score": 0.99}]

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(primary["id"], "q")
    assert result["sources"] == []


async def test_run_chat_turn_related_notes_respects_limit(chat, notes_store, monkeypatch):
    primary = notes_store.add_note("primary", repo="khimaira")
    others = [notes_store.add_note(f"other note body {i}", repo="khimaira") for i in range(5)]

    async def fake_search(query, **kwargs):
        return [{"note_id": o["id"], "score": 0.5} for o in others]

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(primary["id"], "q")
    assert len(result["sources"]) == chat._CHAT_RELATED_NOTES_LIMIT


async def test_run_chat_turn_skips_related_note_deleted_since_indexing(
    chat, notes_store, monkeypatch
):
    primary = notes_store.add_note("primary", repo="khimaira")

    async def fake_search(query, **kwargs):
        return [{"note_id": "deleted-ghost-id", "score": 0.9}]

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(primary["id"], "q")
    assert result["sources"] == []


async def test_run_chat_turn_related_notes_search_failure_degrades_gracefully(
    chat, notes_store, monkeypatch
):
    """A search-backend error (e.g. Qdrant unreachable) must not crash the
    whole chat turn — related-notes injection degrades to 'none found'."""
    primary = notes_store.add_note("primary", repo="khimaira")

    async def fake_search(query, **kwargs):
        raise RuntimeError("qdrant unreachable")

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_retrieval, "search_notes_async", fake_search)
    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)

    result = await chat.run_chat_turn(primary["id"], "q")
    assert result["sources"] == []


async def test_run_chat_turn_no_hits_shows_none_found_in_instruction(chat, notes_store, monkeypatch):
    """Default fixture behavior (no hits at all) — instruction must say so
    explicitly rather than leaving a blank/malformed section, so the model
    doesn't hallucinate related notes that were never actually retrieved."""
    primary = notes_store.add_note("primary", repo="khimaira")
    captured = {}

    async def fake_grounded(content, instruction, *, repo_root, max_budget_usd, schema, **kwargs):
        captured["instruction"] = instruction
        return _grounded(answer="ok")

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_agentic_grounded", fake_grounded)
    result = await chat.run_chat_turn(primary["id"], "q")

    assert chat._NO_RELATED_NOTES in captured["instruction"]
    assert result["sources"] == []


# ---------------------------------------------------------------------------
# schedule_chat_turn / job store integration
# ---------------------------------------------------------------------------


async def test_schedule_chat_turn_works_on_regular_notes(chat, notes_store, monkeypatch):
    """CHAT-UNIFY (2026-07-04): schedule_chat_turn no longer rejects a
    non-guide note — it only fails fast on an UNKNOWN note_id."""
    import asyncio

    from khimaira.monitor import notebook_pipeline

    note = notes_store.add_note("just a note")

    async def fake_run(note_id, message, *, max_budget_usd):
        return {
            "message": {"content": "hi", "edit": None},
            "grounding": {
                "web_grounded": False,
                "web_grounding_unverified": False,
                "code_citations": [],
                "web_citations": [],
            },
            "total_cost_usd": 0.1,
        }

    monkeypatch.setattr(chat, "run_chat_turn", fake_run)
    job_id = chat.schedule_chat_turn(note["id"], "hello")
    await asyncio.sleep(0.05)

    assert notebook_pipeline.get_research_job(job_id)["status"] == "done"


async def test_schedule_chat_turn_unknown_note_raises(chat):
    with pytest.raises(ValueError, match="No note with id"):
        chat.schedule_chat_turn("no-such-note", "hello")


async def test_schedule_chat_turn_completes_and_is_pollable(chat, notes_store, monkeypatch):
    import asyncio

    from khimaira.monitor import notebook_pipeline

    guide = notes_store.add_study_guide("# G\n\nbody\n")

    async def fake_run(note_id, message, *, max_budget_usd):
        return {
            "message": {"content": "hi", "edit": None},
            "grounding": {
                "web_grounded": False,
                "web_grounding_unverified": False,
                "code_citations": [],
                "web_citations": [],
            },
            "total_cost_usd": 0.1,
        }

    monkeypatch.setattr(chat, "run_chat_turn", fake_run)
    job_id = chat.schedule_chat_turn(guide["id"], "hello")

    assert notebook_pipeline.get_research_job(job_id)["status"] == "pending"
    await asyncio.sleep(0.05)

    job = notebook_pipeline.get_research_job(job_id)
    assert job["status"] == "done"
    assert job["kind"] == "chat"
    assert job["message"]["content"] == "hi"


async def test_schedule_chat_turn_reports_error_on_exception(chat, notes_store, monkeypatch):
    import asyncio

    from khimaira.monitor import notebook_pipeline

    guide = notes_store.add_study_guide("# G\n\nbody\n")

    async def failing_run(note_id, message, *, max_budget_usd):
        raise RuntimeError("agentic call blew up")

    monkeypatch.setattr(chat, "run_chat_turn", failing_run)
    job_id = chat.schedule_chat_turn(guide["id"], "hello")
    await asyncio.sleep(0.05)

    job = notebook_pipeline.get_research_job(job_id)
    assert job["status"] == "error"
    assert job["kind"] == "chat"
    assert "agentic call blew up" in job["error"]


# ---------------------------------------------------------------------------
# compact_chat_history
# ---------------------------------------------------------------------------


async def test_compact_below_threshold_is_a_no_op(chat, notes_store):
    guide = notes_store.add_study_guide("# G\n\nbody\n")
    chat.append_chat_messages(guide["id"], {"role": "user", "content": "hi", "ts": "t"})

    result = await chat.compact_chat_history(guide["id"])

    assert result == {"compacted": False, "message_count": 1}
    assert chat.get_chat_history(guide["id"]) == [{"role": "user", "content": "hi", "ts": "t"}]


async def test_compact_above_threshold_summarizes_and_keeps_tail(chat, notes_store, monkeypatch):
    guide = notes_store.add_study_guide("# G\n\nbody\n")
    for i in range(6):
        chat.append_chat_messages(
            guide["id"], {"role": "user", "content": f"msg{i}", "ts": f"t{i}"}
        )

    async def fake_invoke_claude(content, instruction, **kwargs):
        assert "msg0" in content
        return "Summary of the early conversation."

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_claude", fake_invoke_claude)

    result = await chat.compact_chat_history(guide["id"])

    assert result["compacted"] is True
    new_history = chat.get_chat_history(guide["id"])
    assert result["message_count"] == len(new_history)
    assert new_history[0]["role"] == "system"
    assert "Summary of the early conversation." in new_history[0]["content"]
    # last _COMPACT_KEEP_TAIL (4) messages kept verbatim
    assert [m["content"] for m in new_history[1:]] == ["msg2", "msg3", "msg4", "msg5"]


async def test_compact_threads_guide_own_repo_as_target_repo(chat, notes_store, monkeypatch):
    """Personal-context repo-scoping (2026-07-04): compact's summarization
    call is a caller of _invoke_claude too — must thread the guide's own
    repo rather than silently reverting to global-only."""
    guide = notes_store.add_study_guide("# G\n\nbody\n", repo="jeevy_portal")
    for i in range(6):
        chat.append_chat_messages(
            guide["id"], {"role": "user", "content": f"msg{i}", "ts": f"t{i}"}
        )
    captured = {}

    async def fake_invoke_claude(content, instruction, **kwargs):
        captured["target_repo"] = kwargs.get("target_repo")
        return "summary"

    monkeypatch.setattr(chat.notebook_pipeline, "_invoke_claude", fake_invoke_claude)

    await chat.compact_chat_history(guide["id"])
    assert captured["target_repo"] == "jeevy_portal"


async def test_compact_unknown_note_raises(chat):
    with pytest.raises(ValueError, match="No note with id"):
        await chat.compact_chat_history("no-such-note")
