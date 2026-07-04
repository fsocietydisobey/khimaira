"""HTTP client wrappers for the notebook (`/api/notes*`) REST surface,
exposed as MCP tools — the "roster loop" agent-facing layer.

Same separation of concerns as kg_*/session_* in monitor_tools.py: khimaira's
MCP server runs one process per Claude Code session (stdio); the monitor
daemon is a single long-running process (HTTP, :8740) that owns the notes
JSONL store on disk. Calling `khimaira.monitor.notes` in-process from the MCP
layer would mean every connected session's MCP subprocess reads/writes the
same `notes/index.jsonl` + `notes/<id>.json` files directly with no
coordination — exactly the concurrent-writer race this module exists to
avoid for a feature whose whole point is master + the agent roster editing
the same notes concurrently. The daemon is the single writer; these tools
are thin HTTP clients, reusing monitor_tools' `_get`/`_post` error-mapping
and base-url conventions verbatim.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from khimaira.server.monitor_tools import _delete, _get, _patch, _post

# Mirror of notes.PERSONAL_TAB_ID — deliberately NOT imported. This module is a
# pure HTTP client to the daemon; importing the monitor's in-process `notes`
# module here is exactly the coupling the client/daemon split exists to avoid
# (see this file's header docstring). Kept honest by test_notebook_tools'
# personal-tab-constant sync assertion.
_PERSONAL_TAB_ID = "personal"

_LIFECYCLE_BADGE = {
    "captured": "📝",
    "reviewed": "👀",
    "resolved": "✅",
}


def _badge(lifecycle: str) -> str:
    return _LIFECYCLE_BADGE.get(lifecycle, "❓")


def _notes_qs(tab: str = "") -> str:
    return f"?tab_id={urllib.parse.quote(tab)}" if tab else ""


async def notebook_list(project: str = "", tab: str = "") -> str:
    """List notes — read for context before working a problem.

    `project` scopes to one repo's notes client-side (GET /api/notes has no
    server-side repo filter; every stub already carries `repo`, so filtering
    here costs nothing extra). `tab` scopes server-side via `tab_id`.
    """
    data = _get(f"/api/notes{_notes_qs(tab)}")
    if isinstance(data, str):
        return data

    notes = data.get("notes", [])
    if project:
        notes = [n for n in notes if n.get("repo") == project]
    if not notes:
        scope = f" (project={project!r})" if project else ""
        return f"📭 no notes{scope}{f' in tab {tab!r}' if tab else ''}."

    lines = [f"📓 **{len(notes)} note(s)**{f' — project={project!r}' if project else ''}:\n"]
    for n in notes:
        lifecycle = n.get("lifecycle", "captured")
        lines.append(
            f"{_badge(lifecycle)} `{n['id']}` **{n.get('title', '?')}** "
            f"[{lifecycle}]  repo={n.get('repo', '?')} tab={n.get('tab_id', '?')}"
        )
    lines.append(
        "\nUse `notebook_get(note_id)` to read one in full, or "
        "`notebook_add_resolution(note_id, resolution)` once you've worked it."
    )
    return "\n".join(lines)


async def notebook_search(query: str, project: str = "", top_k: int = 5) -> str:
    """Semantic search over notes — find candidates before reading them fully.

    Returns ranked `note_id`s + scores; follow up with `notebook_get` to read
    the full note (this stays cheap by not fetching full bodies for every hit).
    """
    if not query.strip():
        return "❌ notebook_search needs a non-empty query."
    qs = f"?q={urllib.parse.quote(query)}&top_k={top_k}"
    if project:
        qs += f"&repo={urllib.parse.quote(project)}"
    data = _get(f"/api/notes/search{qs}")
    if isinstance(data, str):
        return data

    hits = data.get("hits", [])
    if not hits:
        scope = f" (project={project!r})" if project else ""
        return f"🔍 no notes match {query!r}{scope}."

    lines = [
        f"🔍 **{len(hits)} match(es) for {query!r}**{f' in {project!r}' if project else ''}:\n"
    ]
    for h in hits:
        lines.append(f"  • `{h.get('note_id', '?')}`  score={h.get('score', '?')}")
    lines.append("\nUse `notebook_get(note_id)` to read one in full.")
    return "\n".join(lines)


async def notebook_get(note_id: str) -> str:
    """Read one note in full — title, raw paste, structured pipeline output
    (if processed), and any existing resolution."""
    if not note_id:
        return "❌ notebook_get requires a note_id — get one from notebook_list/notebook_search."
    data = _get(f"/api/notes/{urllib.parse.quote(note_id, safe='')}")
    if isinstance(data, str):
        return data

    lifecycle = data.get("lifecycle") or ("resolved" if data.get("resolution") else "captured")
    lines = [
        f"{_badge(lifecycle)} **{data.get('title', '?')}**  `{data['id']}`  [{lifecycle}]",
        f"repo={data.get('repo', '?')}  tab={data.get('tab_id', '?')}  "
        f"status={data.get('status', '?')}\n",
        f"**Raw paste:**\n{data.get('raw_text', '')}\n",
    ]
    pipeline = data.get("pipeline")
    if pipeline:
        lines.append(f"**Summary:** {pipeline.get('summary', '')}")
        lines.append(f"\n**Organized:**\n{pipeline.get('organized_md', '')}\n")
    resolution = data.get("resolution")
    if resolution:
        lines.append(
            f"**Resolution** (by {data.get('resolved_by') or '(unattributed)'} "
            f"at {data.get('resolved_at')}):\n{resolution}"
        )
    else:
        lines.append(
            "**No resolution yet.** Once you've worked this, write one back with "
            "`notebook_add_resolution(note_id, resolution, resolved_by=<you>)` — "
            "that's what promotes the note to training data."
        )
    return "\n".join(lines)


async def notebook_ask(question: str, project: str = "") -> str:
    """Ask a code-grounded question against the notebook.

    Retrieves candidate notes, re-validates each against the CURRENT code
    (self-healing — a stale note gets corrected before it's used), then
    synthesizes an answer citing the notes it drew on. This is the tool to
    reach for when you want an ANSWER, not a list of notes to read yourself.
    """
    if not question.strip():
        return "❌ notebook_ask needs a non-empty question."
    body: dict[str, Any] = {"question": question}
    if project:
        body["repo"] = project
    # answer_question's staleness-gated revalidate loop can shell out to a
    # headless `claude -p` per stale hit — give it real headroom, not the
    # 5s default (mirrors kg_graph/kg_node's own 30s override for
    # comparably expensive daemon-side work).
    data = _post("/api/notes/ask", body, timeout=180.0)
    if isinstance(data, str):
        return data

    answer = data.get("answer", "")
    sources = data.get("sources") or []
    healed = data.get("healed") or []
    lines = [f"💬 {answer}"]
    if sources:
        lines.append(f"\n**Sources:** {', '.join(f'`{s}`' for s in sources)}")
    if healed:
        lines.append(
            f"**Healed (were stale, just corrected):** {', '.join(f'`{h}`' for h in healed)}"
        )
    return "\n".join(lines)


async def notebook_add_resolution(note_id: str, resolution: str, resolved_by: str = "") -> str:
    """Write a resolution back to a note — call this once you've finished
    working the problem it describes.

    This is the roster-loop write-back: the {problem, resolution} pair is
    what promotes the note's lifecycle to "resolved" and fires a
    fire-and-forget mnemosyne distill so it feeds the next oracle re-bake.
    `resolved_by` should be your session name/id — it's attributed on the
    note and carried into the training pair's provenance.
    """
    if not note_id:
        return "❌ notebook_add_resolution requires a note_id."
    if not resolution.strip():
        return "❌ notebook_add_resolution requires a non-empty resolution."
    data = _post(
        f"/api/notes/{urllib.parse.quote(note_id, safe='')}/resolution",
        {"resolution": resolution, "resolved_by": resolved_by},
    )
    if isinstance(data, str):
        return data
    return (
        f"✅ resolution added to `{data['id']}` — **{data.get('title', '?')}** "
        f"is now [resolved] (by {resolved_by or '(unattributed)'}). "
        f"Queued for mnemosyne distillation."
    )


async def notebook_update(
    note_id: str,
    title: str = "",
    tab_id: str = "",
    raw_text: str = "",
    status: str = "",
    repo: str = "",
) -> str:
    """Edit a note's title/tab/raw_text/status/repo. Only pass the fields
    you want to change — omitted args (empty string) are left untouched.

    For attaching a resolution, use `notebook_add_resolution` instead — it's
    a dedicated endpoint that also fires the training write-back.
    """
    if not note_id:
        return "❌ notebook_update requires a note_id."
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    if tab_id:
        body["tab_id"] = tab_id
    if raw_text:
        body["raw_text"] = raw_text
    if status:
        body["status"] = status
    if repo:
        body["repo"] = repo
    if not body:
        return "❌ notebook_update needs at least one field to change."
    data = _patch(f"/api/notes/{urllib.parse.quote(note_id, safe='')}", body)
    if isinstance(data, str):
        return data
    return f"✅ `{data['id']}` updated — **{data.get('title', '?')}**."


async def notebook_revalidate(note_id: str) -> str:
    """Re-ground a note against the CURRENT code and re-run its structuring
    pipeline — the manual reprocess/resync fallback.

    Normally you never need this: notebook mutations auto-resync at the daemon
    (`notebook_create` and `raw_text` edits via `notebook_update` re-run the
    structuring pipeline; `notebook_delete` / `notebook_add_resolution` handle
    their own resyncs). Reach for this only when you OBSERVE a note whose
    structured tabs look stale versus its source, or the code it's validated
    against has moved on — it re-derives summary/technical/plain and heals the
    note if the code drifted. Slower than the other notebook_* tools (spawns a
    headless `claude -p` re-check), so it's awaited with a longer timeout.
    """
    if not note_id:
        return "❌ notebook_revalidate requires a note_id — get one from notebook_list/notebook_search."
    data = _post(
        f"/api/notes/{urllib.parse.quote(note_id, safe='')}/revalidate",
        {},
        timeout=180.0,
    )
    if isinstance(data, str):
        return data
    heals = len(data.get("history") or [])
    return (
        f"🔄 revalidated `{data.get('id', note_id)}` — **{data.get('title', '?')}** "
        f"[{data.get('status', '?')}], re-checked vs code at "
        f"{data.get('last_validated_at', '?')}"
        + (f" · {heals} heal(s) in history" if heals else "")
        + "."
    )


async def notebook_create(
    project: str = "",
    raw_text: str = "",
    title: str = "",
    tab: str = "",
) -> str:
    """Capture a NEW note into the notebook — the roster loop's write-IN half.

    Reach for this to persist a finding, design hesitation, bug-class result,
    or decision produced during work into the durable, code-grounded notebook,
    where it can later be @-mentioned, asked against (`notebook_ask`), and —
    once resolved (`notebook_add_resolution`) — promoted to oracle training
    data. `create` captures the problem; `add_resolution` closes it.

    `project` is the repo the note is validated against (e.g. "khimaira",
    "jeevy_portal") — pass it EXPLICITLY so the note isn't mis-scoped to the
    default repo. `tab` is an optional folder; omit for the General bucket.
    The note returns immediately as a draft — the structuring pipeline
    (summary / organized / technical / plain + a derived title) runs async,
    exactly like a UI paste, so this call does not block on the LLM transform.
    """
    if not raw_text.strip():
        return "❌ notebook_create requires non-empty raw_text — the note body to capture."
    if tab == _PERSONAL_TAB_ID:
        return (
            "❌ refusing to create into the 'personal' tab — that's Joseph's "
            "behavior/voice folder, read verbatim into every notebook LLM call. "
            "Capture roster notes into the General bucket (omit `tab`) or a "
            "named tab instead."
        )
    body: dict[str, Any] = {"raw_text": raw_text}
    if title:
        body["title"] = title
    if tab:
        body["tab_id"] = tab
    if project:
        body["repo"] = project
    data = _post("/api/notes", body)
    if isinstance(data, str):
        return data
    return (
        f"📝 captured `{data['id']}` — **{data.get('title', '?')}** "
        f"[{data.get('status', 'draft')}]  repo={data.get('repo', '?')} "
        f"tab={data.get('tab_id', '?')}. Structuring pipeline running async — "
        f"read it back with `notebook_get(note_id)` shortly, or attach a "
        f"resolution with `notebook_add_resolution` once you've worked it."
    )


async def notebook_delete(note_id: str) -> str:
    """Delete a note permanently. DESTRUCTIVE — the note file is removed (a
    deleted-tombstone remains in the index, but the content is gone; the
    notebook store has no git-history safety net like the source tree does).

    Guarded: reads the note first (so the confirmation names what was removed)
    and REFUSES to delete anything in the 'personal' tab — that's Joseph's
    user-owned behavior/voice folder, not roster-deletable. Move a note out of
    the personal tab first if deletion is genuinely intended.
    """
    if not note_id:
        return "❌ notebook_delete requires a note_id — get one from notebook_list/notebook_search."
    quoted = urllib.parse.quote(note_id, safe="")
    existing = _get(f"/api/notes/{quoted}")
    if isinstance(existing, str):
        return existing  # 404 / daemon-down — already a formatted error string
    if existing.get("tab_id") == _PERSONAL_TAB_ID:
        return (
            f"❌ refusing to delete `{note_id}` — it's in the 'personal' tab "
            "(Joseph's behavior/voice folder, user-owned). Move it out of the "
            "personal tab first if deletion is really intended."
        )
    title = existing.get("title", "?")
    data = _delete(f"/api/notes/{quoted}")
    if isinstance(data, str):
        return data
    return (
        f"🗑️ deleted `{note_id}` — **{title}** "
        f"(repo={existing.get('repo', '?')}, tab={existing.get('tab_id', '?')})."
    )
