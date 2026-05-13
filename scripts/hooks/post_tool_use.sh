#!/usr/bin/env bash
# khimaira PostToolUse hook — auto-log file touches to khimaira's session store.
#
# Triggered after Edit / Write / MultiEdit / NotebookEdit. Reads the hook
# JSON from stdin, extracts file path + tool name, fires session_log_touch
# against the khimaira-monitor daemon's REST API.
#
# Design constraints:
#   - Must NOT block Claude Code. Silent-fail on every error (daemon down,
#     jq missing, malformed input). Output nothing on success.
#   - Must NOT make API calls (that path was the fire_swarm scenario).
#     Pure local HTTP to 127.0.0.1:8740. No surprises.
#   - Idempotent + safe to re-run. Logs are append-only JSONL.

set -u

# Tunables — overridable via env
KHIMAIRA_HOOK_BASE_URL="${KHIMAIRA_HOOK_BASE_URL:-http://127.0.0.1:8740}"
KHIMAIRA_HOOK_TIMEOUT="${KHIMAIRA_HOOK_TIMEOUT:-2}"

# Need jq + curl. If either is missing, silent-skip — don't block.
command -v jq >/dev/null 2>&1 || exit 0
command -v curl >/dev/null 2>&1 || exit 0

# Read stdin (Claude Code's hook payload).
INPUT="$(cat)"
[ -z "$INPUT" ] && exit 0

# Extract fields. jq returns "null" string if missing — guard for that.
SESSION_ID="$(echo "$INPUT" | jq -r '.session_id // empty')"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // empty')"
[ -z "$SESSION_ID" ] && exit 0
[ -z "$TOOL_NAME" ] && exit 0

# Only log file-mutating tools.
case "$TOOL_NAME" in
    Edit|Write|MultiEdit|NotebookEdit) ;;
    *) exit 0 ;;
esac

# File path lives at .tool_input.file_path for Edit/Write/NotebookEdit and
# .tool_input.edits[*].file_path for MultiEdit. Handle both.
FILE_PATH="$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')"
if [ -z "$FILE_PATH" ]; then
    # MultiEdit — log each unique file_path separately
    FILES="$(echo "$INPUT" | jq -r '.tool_input.edits[]?.file_path // empty' | sort -u)"
    [ -z "$FILES" ] && exit 0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        _post_touch() {
            curl -s --max-time "$KHIMAIRA_HOOK_TIMEOUT" -X POST \
                "$KHIMAIRA_HOOK_BASE_URL/api/sessions/$SESSION_ID/touch" \
                -H "Content-Type: application/json" \
                -d "$(jq -n --arg f "$1" --arg t "$TOOL_NAME" '{file: $f, summary: ("auto-logged from " + $t + " hook")}')" \
                >/dev/null 2>&1 || true
        }
        _post_touch "$f"
    done <<< "$FILES"
    exit 0
fi

# Single-file path
PAYLOAD="$(jq -n \
    --arg f "$FILE_PATH" \
    --arg t "$TOOL_NAME" \
    '{file: $f, summary: ("auto-logged from " + $t + " hook")}')"

curl -s --max-time "$KHIMAIRA_HOOK_TIMEOUT" -X POST \
    "$KHIMAIRA_HOOK_BASE_URL/api/sessions/$SESSION_ID/touch" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    >/dev/null 2>&1 || true

exit 0
