#!/usr/bin/env bash
# khimaira SessionStart hook — fetch unread inbox notes for this session.
#
# Triggered when Claude Code starts a session (startup, resume, or clear).
# Calls session_pending_notes(session_id) — if other sessions have posted
# answers to this session's open questions, we surface them in stdout so
# Claude reads "session B answered Q3" without the user having to ask.
#
# Output goes to stdout in JSON format that Claude Code's hook system
# interprets (additional context for the next turn).

set -u

KHIMAIRA_HOOK_BASE_URL="${KHIMAIRA_HOOK_BASE_URL:-http://127.0.0.1:8740}"
KHIMAIRA_HOOK_TIMEOUT="${KHIMAIRA_HOOK_TIMEOUT:-2}"

command -v jq >/dev/null 2>&1 || exit 0
command -v curl >/dev/null 2>&1 || exit 0

INPUT="$(cat)"
SESSION_ID="$(echo "$INPUT" | jq -r '.session_id // empty')"
[ -z "$SESSION_ID" ] && exit 0

# Fetch pending notes (mark_read=true so they consume on first read).
NOTES_JSON="$(curl -s --max-time "$KHIMAIRA_HOOK_TIMEOUT" \
    "$KHIMAIRA_HOOK_BASE_URL/api/sessions/$SESSION_ID/pending?mark_read=true" 2>/dev/null)"

# If daemon's down or response malformed, exit silently.
[ -z "$NOTES_JSON" ] && exit 0

# Count notes. Empty list → exit silently.
NOTE_COUNT="$(echo "$NOTES_JSON" | jq -r '.notes | length // 0' 2>/dev/null)"
[ -z "$NOTE_COUNT" ] || [ "$NOTE_COUNT" = "0" ] && exit 0

# Format the inbox into a context block Claude will see.
INBOX_TEXT="$(echo "$NOTES_JSON" | jq -r '
    "📬 khimaira inbox — " + (.notes | length | tostring) + " unread answer(s) from other sessions:\n\n" +
    (.notes | map(
        "- (from " + (.from_session_id // "unknown") + ")\n" +
        "  Q: " + (.question_text // "?") + "\n" +
        "  A: " + (.answer // "?") + "\n"
    ) | join("\n"))
' 2>/dev/null)"

[ -z "$INBOX_TEXT" ] && exit 0

# Claude Code's SessionStart hook reads JSON from stdout. The
# `additionalContext` field gets injected into the model's context for
# the upcoming turn.
jq -n --arg ctx "$INBOX_TEXT" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'

exit 0
