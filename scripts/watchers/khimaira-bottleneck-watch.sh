#!/usr/bin/env bash
# khimaira-bottleneck-watch — detect master-as-bottleneck conditions and escalate.
#
# v1.7 — Three-tier escalation:
#   T1 (first detection): notify-send + PushNotification with explicit
#       /model sonnet | /effort medium | /khimaira-deputize commands.
#   T2 (bottleneck persists ≥ AUTO_DEPUTIZE_AFTER_MIN): auto-fire
#       chat_transfer_membership(..., as_deputize=true) directly via daemon
#       HTTP for each chat the master is creator of. Bypasses the rate-
#       limited master entirely. Opt-out via KHIMAIRA_AUTO_DEPUTIZE=0.
#   T3 (cleared): state files removed; full cycle resets.
#
# Heuristic: poll the khimaira-monitor daemon for sessions whose status is
# "awaiting-review" for > THRESHOLD_MIN minutes AND whose most-recent
# session_log_decision is older than DECISION_STALE_MIN minutes. When both
# conditions hold for N>=MIN_BOTTLENECKED sessions simultaneously, master
# is plausibly the bottleneck.
#
# Installed via systemd user timer: `khimaira-bottleneck-watch.timer`.
# State files (under STATE_DIR = ~/.local/state/khimaira/):
#   - bottleneck-watch.last-alert   — T1 notify cooldown (epoch ts)
#   - bottleneck-watch.first-seen   — T1 first-detection ts (drives T2 trigger)
#   - bottleneck-watch.last-deputize — T2 cooldown (epoch ts)
# Log: bottleneck-watch.log
#
# To disable T2 only: `KHIMAIRA_AUTO_DEPUTIZE=0` (in the unit file or env).
# To disable entirely: `systemctl --user disable --now khimaira-bottleneck-watch.timer`.
# To reset state: `rm ~/.local/state/khimaira/bottleneck-watch.*`.

set -euo pipefail

STATE_DIR="$HOME/.local/state/khimaira"
LAST_ALERT_FILE="$STATE_DIR/bottleneck-watch.last-alert"
FIRST_SEEN_FILE="$STATE_DIR/bottleneck-watch.first-seen"
LAST_DEPUTIZE_FILE="$STATE_DIR/bottleneck-watch.last-deputize"
TIMESTAMP=$(date -Iseconds)
NOW_EPOCH=$(date +%s)

# Tuning knobs.
THRESHOLD_MIN=30              # session in awaiting-review for ≥ this long
DECISION_STALE_MIN=20         # master's last decision older than this
MIN_BOTTLENECKED=2            # need ≥ this many sessions to fire
NOTIFY_COOLDOWN_MIN=60        # T1 notify cadence
AUTO_DEPUTIZE_AFTER_MIN=15    # T2 fires when first-seen is ≥ this old
DEPUTIZE_COOLDOWN_MIN=120     # T2 cooldown to prevent flap

# Opt-out env var for T2 auto-deputize. Set to "0" or "false" to disable.
AUTO_DEPUTIZE_ENABLED="${KHIMAIRA_AUTO_DEPUTIZE:-1}"

mkdir -p "$STATE_DIR"

# --- Query daemon ---
DAEMON_URL="http://127.0.0.1:8740"
sessions_json=$(curl -sf --max-time 5 "$DAEMON_URL/api/sessions" 2>/dev/null) || {
  echo "[$TIMESTAMP] daemon unreachable at $DAEMON_URL — skip"
  exit 0
}

# --- Analyze bottleneck ---
analysis=$(python3 - <<PYEOF
import json
import sys
from datetime import datetime, timezone, timedelta

threshold = timedelta(minutes=$THRESHOLD_MIN)
decision_stale = timedelta(minutes=$DECISION_STALE_MIN)
now = datetime.now(timezone.utc)
data = json.loads("""$sessions_json""")
sessions = data if isinstance(data, list) else data.get("sessions", [])

awaiting_count = 0
master_stale = False
master_name = None
master_sid = None
master_decision_age_min = None

for s in sessions:
    status = s.get("status", "")
    last_active = s.get("last_active_at") or s.get("updated_at") or ""
    if not last_active:
        continue
    try:
        if last_active.endswith("Z"):
            last_active = last_active[:-1] + "+00:00"
        last_dt = datetime.fromisoformat(last_active)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        continue

    age = now - last_dt
    if status == "awaiting-review" and age > threshold:
        awaiting_count += 1
    if status == "orchestrating":
        recent_decisions = s.get("recent_decisions") or []
        if recent_decisions:
            last_decision_ts = recent_decisions[0].get("ts", "")
            try:
                if last_decision_ts.endswith("Z"):
                    last_decision_ts = last_decision_ts[:-1] + "+00:00"
                ldt = datetime.fromisoformat(last_decision_ts)
                if ldt.tzinfo is None:
                    ldt = ldt.replace(tzinfo=timezone.utc)
                decision_age = now - ldt
                if decision_age > decision_stale:
                    master_stale = True
                    master_name = s.get("name") or (s.get("session_id", "") or "")[:8]
                    master_sid = s.get("session_id", "")
                    master_decision_age_min = int(decision_age.total_seconds() / 60)
            except (ValueError, TypeError):
                pass

bottlenecked = awaiting_count >= $MIN_BOTTLENECKED and master_stale
print(json.dumps({
    "bottlenecked": bottlenecked,
    "awaiting_count": awaiting_count,
    "master_name": master_name,
    "master_sid": master_sid,
    "master_decision_age_min": master_decision_age_min,
}))
PYEOF
)

bottlenecked=$(echo "$analysis" | python3 -c "import sys, json; print(json.load(sys.stdin)['bottlenecked'])")
awaiting_count=$(echo "$analysis" | python3 -c "import sys, json; print(json.load(sys.stdin)['awaiting_count'])")
master_name=$(echo "$analysis" | python3 -c "import sys, json; print(json.load(sys.stdin).get('master_name') or '')")
master_sid=$(echo "$analysis" | python3 -c "import sys, json; print(json.load(sys.stdin).get('master_sid') or '')")
decision_age=$(echo "$analysis" | python3 -c "import sys, json; print(json.load(sys.stdin).get('master_decision_age_min') or '')")

# --- T3: clear state if bottleneck has lifted ---
if [[ "$bottlenecked" != "True" ]]; then
  if [[ -f "$FIRST_SEEN_FILE" ]] || [[ -f "$LAST_ALERT_FILE" ]] || [[ -f "$LAST_DEPUTIZE_FILE" ]]; then
    echo "[$TIMESTAMP] T3: bottleneck cleared — resetting state files"
    rm -f "$FIRST_SEEN_FILE" "$LAST_ALERT_FILE" "$LAST_DEPUTIZE_FILE"
  else
    echo "[$TIMESTAMP] no bottleneck (awaiting=$awaiting_count)"
  fi
  exit 0
fi

# --- Record first-seen if not already tracking ---
if [[ ! -f "$FIRST_SEEN_FILE" ]]; then
  echo "$NOW_EPOCH" > "$FIRST_SEEN_FILE"
  echo "[$TIMESTAMP] FIRST-SEEN: tracking new bottleneck episode"
fi

first_seen_epoch=$(cat "$FIRST_SEEN_FILE")
elapsed_min=$(( (NOW_EPOCH - first_seen_epoch) / 60 ))

# --- T1: notify with concrete suggestions (cooldown-gated) ---
notify_now="false"
if [[ ! -f "$LAST_ALERT_FILE" ]]; then
  notify_now="true"
else
  last_alert_epoch=$(date -d "$(cat "$LAST_ALERT_FILE")" +%s 2>/dev/null || echo 0)
  alert_age_min=$(( (NOW_EPOCH - last_alert_epoch) / 60 ))
  if [[ "$alert_age_min" -ge "$NOTIFY_COOLDOWN_MIN" ]]; then
    notify_now="true"
  fi
fi

if [[ "$notify_now" == "true" ]]; then
  vice_name="${master_name:-master}-vice"
  remaining_to_t2=$(( AUTO_DEPUTIZE_AFTER_MIN - elapsed_min ))
  notify_title="khimaira: master bottleneck ($elapsed_min min)"
  notify_body="$awaiting_count session(s) awaiting review; master $master_name idle ${decision_age}m.

Quick options (type in master window):
  /model sonnet         — drop model tier
  /effort medium        — drop thinking tier
  /khimaira-deputize $vice_name 'rate-limit'

T2 auto-deputize fires in $remaining_to_t2 min if still bottlenecked."
  echo "[$TIMESTAMP] T1: $awaiting_count awaiting; master=$master_name stale=${decision_age}m elapsed=${elapsed_min}m"
  if command -v notify-send &>/dev/null; then
    notify-send -u critical "$notify_title" "$notify_body"
  fi

  # --- T1.b: fan-out session_post_notice to all active sessions ---
  # Surfaces the prompt in each session's UserPromptSubmit hook context on
  # next turn — the load-bearing piece that closes the "I didn't get any
  # prompts in the other sessions" gap. Desktop notify is one channel; the
  # in-session inbox is the other (and the one the agents actually surface
  # back to the user during work).
  notice_body=$(cat <<NOTICE
🎚️ khimaira rate-limit signal — bottleneck detected ($elapsed_min min elapsed).

$awaiting_count session(s) awaiting review; master ${master_name} idle ${decision_age}m. Recommended actions (type in this window now):

  /model sonnet      — drop model tier (~5x cost reduction)
  /effort medium     — drop thinking tier (~5-10x reduction)

If you're the master, also consider:
  /khimaira-deputize ${vice_name} 'rate-limit'

T2 auto-deputize fires in ${remaining_to_t2} min if conditions persist. To opt out: KHIMAIRA_AUTO_DEPUTIZE=0 in env.
NOTICE
)
  # Iterate active sessions in the daemon list and POST notices.
  active_sids=$(echo "$analysis" | python3 - "$sessions_json" <<'PYEOF'
import json, sys
from datetime import datetime, timezone, timedelta

raw_analysis = sys.stdin.read()
sessions_raw = sys.argv[1]
data = json.loads(sessions_raw)
sessions = data if isinstance(data, list) else data.get("sessions", [])
now = datetime.now(timezone.utc)
cutoff = timedelta(minutes=30)

out = []
for s in sessions:
    sid = s.get("session_id") or ""
    if not sid:
        continue
    age_s = s.get("last_active_age_s")
    if age_s is not None:
        if age_s > 1800:
            continue
    else:
        # Fall back to parsing last_active_at if age_s not present
        la = s.get("last_active_at") or s.get("updated_at") or ""
        if not la:
            continue
        try:
            if la.endswith("Z"):
                la = la[:-1] + "+00:00"
            dt = datetime.fromisoformat(la)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt) > cutoff:
                continue
        except (ValueError, TypeError):
            continue
    out.append(sid)
print("\n".join(out))
PYEOF
)

  notice_count=0
  while IFS= read -r sid; do
    [[ -z "$sid" ]] && continue
    # POST the notice. `from_session_id="bottleneck-watch"` makes the source
    # attribution clear in the recipient's inbox.
    if curl -sf -X POST --max-time 5 \
        "$DAEMON_URL/api/sessions/$sid/notice" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "
import json, sys
body = '''$notice_body'''
print(json.dumps({'from_session_id': 'bottleneck-watch', 'text': body}))
")" > /dev/null 2>&1; then
      notice_count=$(( notice_count + 1 ))
    fi
  done <<< "$active_sids"
  echo "[$TIMESTAMP] T1.b: posted notices to $notice_count active session(s)"

  echo "$TIMESTAMP" > "$LAST_ALERT_FILE"
fi

# --- T2: auto-deputize if persistent + enabled + not in cooldown ---
if [[ "$elapsed_min" -lt "$AUTO_DEPUTIZE_AFTER_MIN" ]]; then
  echo "[$TIMESTAMP] T2: not yet (elapsed=${elapsed_min}m < ${AUTO_DEPUTIZE_AFTER_MIN}m)"
  exit 0
fi

if [[ "$AUTO_DEPUTIZE_ENABLED" == "0" || "$AUTO_DEPUTIZE_ENABLED" == "false" ]]; then
  echo "[$TIMESTAMP] T2: auto-deputize disabled (KHIMAIRA_AUTO_DEPUTIZE=$AUTO_DEPUTIZE_ENABLED); skip"
  exit 0
fi

if [[ -f "$LAST_DEPUTIZE_FILE" ]]; then
  last_dep_epoch=$(date -d "$(cat "$LAST_DEPUTIZE_FILE")" +%s 2>/dev/null || echo 0)
  dep_age_min=$(( (NOW_EPOCH - last_dep_epoch) / 60 ))
  if [[ "$dep_age_min" -lt "$DEPUTIZE_COOLDOWN_MIN" ]]; then
    echo "[$TIMESTAMP] T2: deputize cooldown active ($dep_age_min min < $DEPUTIZE_COOLDOWN_MIN min); skip"
    exit 0
  fi
fi

if [[ -z "$master_sid" ]]; then
  echo "[$TIMESTAMP] T2: no master_sid available; cannot deputize. skip"
  exit 0
fi

vice_name="${master_name}-vice"
echo "[$TIMESTAMP] T2: firing auto-deputize for master=$master_name sid=${master_sid:0:8} → vice=$vice_name"

# --- T2.a: push spawn-request ---
push_msg="khimaira auto-deputize firing: spawn $vice_name (master $master_name bottlenecked ${elapsed_min}m)"
if command -v notify-send &>/dev/null; then
  notify-send -u critical "khimaira: AUTO-deputize firing" "$push_msg

Open a new Claude Code window in this project + run session_set_name(name='$vice_name'). Transfer will fire when vice registers (60s timeout)."
fi
# Daemon-side PushNotification analog — write to a known location for any
# session to pick up. Skipped here; the desktop notify is the primary channel.

# --- T2.b: poll for vice registration (60s) ---
vice_sid=""
for i in {1..12}; do
  sleep 5
  vice_resp=$(curl -sf --max-time 3 "$DAEMON_URL/api/sessions/$vice_name" 2>/dev/null) || continue
  vice_sid=$(echo "$vice_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
  if [[ -n "$vice_sid" ]]; then
    echo "[$TIMESTAMP] T2.b: vice registered (sid=${vice_sid:0:8}) at iter=$i"
    break
  fi
done

if [[ -z "$vice_sid" ]]; then
  echo "[$TIMESTAMP] T2.b: vice never registered within 60s; aborting auto-deputize"
  if command -v notify-send &>/dev/null; then
    notify-send -u critical "khimaira: auto-deputize TIMEOUT" \
      "Vice $vice_name never spawned. Rerun /khimaira-deputize manually if needed."
  fi
  exit 0
fi

# --- T2.c: enumerate master's chats + fire transfer-membership per chat ---
CHATS_DIR="$HOME/.local/state/khimaira/chats"
transferred=0
skipped=0
failed=0

if [[ -d "$CHATS_DIR" ]]; then
  for chat_jsonl in "$CHATS_DIR"/*.jsonl; do
    [[ -f "$chat_jsonl" ]] || continue
    chat_id=$(basename "$chat_jsonl" .jsonl)
    # Determine if master is the creator (v1-era fallback for chats without
    # explicit member_roles) OR holds master role explicitly.
    is_master=$(python3 - "$chat_jsonl" "$master_sid" <<'PYEOF'
import json
import sys

path, master_sid = sys.argv[1], sys.argv[2]
last_meta = None
try:
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("kind") == "meta":
                    last_meta = r
            except json.JSONDecodeError:
                continue
except OSError:
    print("no")
    sys.exit(0)

if not last_meta:
    print("no")
    sys.exit(0)

member_roles = last_meta.get("member_roles") or {}
if member_roles.get(master_sid) == "master":
    print("yes")
elif not member_roles and last_meta.get("created_by") == master_sid:
    # v1-era implicit master
    print("yes")
else:
    print("no")
PYEOF
)
    if [[ "$is_master" != "yes" ]]; then
      skipped=$(( skipped + 1 ))
      continue
    fi
    # Fire deputize transfer via daemon HTTP directly.
    transfer_resp=$(curl -sf -X POST \
      "$DAEMON_URL/api/chats/$chat_id/transfer-membership" \
      -H "Content-Type: application/json" \
      -d "$(python3 -c "import json; print(json.dumps({'from_session_id': '$master_sid', 'to_session_id': '$vice_sid', 'as_deputize': True}))")" \
      2>&1) && {
        echo "[$TIMESTAMP] T2.c: transferred $chat_id"
        transferred=$(( transferred + 1 ))
      } || {
        echo "[$TIMESTAMP] T2.c: FAILED $chat_id: $transfer_resp"
        failed=$(( failed + 1 ))
      }
  done
fi

# --- T2.d: notify completion + flip master status to "paused" via daemon ---
curl -sf -X POST --max-time 5 \
  "$DAEMON_URL/api/sessions/$master_sid/status" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "import json; print(json.dumps({'status': 'paused', 'detail': 'paused | auto-deputized to $vice_name (bottleneck-watch T2) | rate-limit'}))")" \
  > /dev/null 2>&1 || echo "[$TIMESTAMP] T2.d: status flip failed (non-fatal)"

echo "[$TIMESTAMP] T2 complete: transferred=$transferred skipped=$skipped failed=$failed; vice=$vice_name"
if command -v notify-send &>/dev/null; then
  notify-send -u critical "khimaira: AUTO-deputize complete" \
    "Master $master_name → $vice_name. Transferred $transferred chat(s) (skipped $skipped, failed $failed). Master is paused; resume via /khimaira-resume in master window."
fi

echo "$TIMESTAMP" > "$LAST_DEPUTIZE_FILE"
