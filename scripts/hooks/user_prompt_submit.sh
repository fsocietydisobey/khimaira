#!/usr/bin/env bash
# khimaira UserPromptSubmit hook — periodic decision/question reminder.
#
# Triggered before each user prompt is processed. Every Nth invocation
# (default: 8) emits a soft reminder that the agent should externalize
# decisions and open questions via session_log_decision /
# session_log_question. Avoids agent-side amnesia about the multi-session
# memory feature.
#
# Note: we deliberately DO NOT auto-extract decisions from prose. Agents
# tested poorly at recognizing "this was a decision" — manual logging
# stays manual. We just nudge.
#
# Counter is per-session, persisted at:
#   ~/.local/state/khimaira/hook-counters/<session_id>.count
# Counter resets on overflow (defensive, not a real risk).

set -u

KHIMAIRA_HOOK_BASE_URL="${KHIMAIRA_HOOK_BASE_URL:-http://127.0.0.1:8740}"
KHIMAIRA_HOOK_REMINDER_EVERY="${KHIMAIRA_HOOK_REMINDER_EVERY:-8}"

command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat)"
SESSION_ID="$(echo "$INPUT" | jq -r '.session_id // empty')"
[ -z "$SESSION_ID" ] && exit 0

COUNTER_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/khimaira/hook-counters"
mkdir -p "$COUNTER_DIR" 2>/dev/null || exit 0
COUNTER_FILE="$COUNTER_DIR/${SESSION_ID}.count"

# Read counter (default 0)
if [ -r "$COUNTER_FILE" ]; then
    COUNT="$(cat "$COUNTER_FILE" 2>/dev/null)"
else
    COUNT=0
fi
case "$COUNT" in
    ''|*[!0-9]*) COUNT=0 ;;
esac

NEW_COUNT=$((COUNT + 1))
echo "$NEW_COUNT" > "$COUNTER_FILE.tmp" && mv "$COUNTER_FILE.tmp" "$COUNTER_FILE"

# Only fire reminder every N turns (and never on turn 1 — let the agent
# settle in)
if [ "$NEW_COUNT" -lt 2 ] || [ $((NEW_COUNT % KHIMAIRA_HOOK_REMINDER_EVERY)) -ne 0 ]; then
    exit 0
fi

# Emit a soft reminder block as additional context for the model.
REMINDER='💡 khimaira reminder: any new decisions or open questions worth logging? Use `session_log_decision(session_id="'"$SESSION_ID"'", text=...)` for commitments and `session_log_question(session_id="'"$SESSION_ID"'", text=...)` for things you want a parallel session to research. Skip if nothing to log.'

jq -n --arg ctx "$REMINDER" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'

exit 0
