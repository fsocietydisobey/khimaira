# Desktop Notifications — push cross-session events to user

> **Status:** Spec'd, not started.
> Build when: you start losing track of which Claude window has unread
> inbox / incoming questions because you're context-switched away. Until
> then, the auto-inject hook surfaces things on next prompt anyway.

## Problem

Today's cross-session coordination loop:

1. Session A logs a targeted question → lands as `📨 khimaira incoming` in
   session B's UserPromptSubmit hook output
2. **You have to type something in B's window to trigger the hook.** Until
   then, the message is invisible.

If you're heads-down in a different terminal, on a different monitor, or
afk, you don't know there's something waiting. The "tap to wake B" step
becomes the bottleneck.

Vue / Slack / iMessage solve this with desktop notifications. Khimaira
should too.

## Design

### Architecture

```
[ khimaira-monitor daemon ]
        ↓ SSE (new endpoint)
[ khimaira-notifier — small standalone daemon ]
        ↓ libnotify / osascript / win-toast
[ user's desktop notification tray ]
        ↓ click
[ wakes the matching terminal / Claude session ]
```

### New khimaira-monitor endpoint

```
GET /api/sessions/notifications/stream
  → SSE stream of { event, session_id, payload, ts }
  Events:
    - inbox_note      (post_notice or post_answer landed)
    - incoming_question (targeted question logged)
    - handoff         (handoff scoped to a cwd you have a session in)
```

Reuse the existing SSE infrastructure (heartbeats already has SSE).

### New khimaira-notifier subprocess

Standalone Python daemon, ~80 LOC. Subscribes to the SSE stream, fires
desktop notifications via:

- **Linux:** `notify-send "📬 llm-piping-extension has 1 new note"`
  (libnotify is universal; no extra deps)
- **macOS:** `osascript -e 'display notification "..." with title "khimaira"'`
- **Windows:** `winsdk.ui.notifications` or `win10toast`

Click action: write the session_id to `~/.local/state/khimaira/last-clicked.txt`
(no API to focus a terminal cross-platform; user picks up from there).

### Configuration

```toml
# ~/.config/khimaira/notifier.toml
[notifier]
enabled = true
filter_session_ids = []         # empty = all sessions
filter_event_types = ["incoming_question", "handoff"]  # skip routine notes
quiet_hours = { from = "22:00", to = "08:00" }
```

### Distribution

`khimaira notifier` subcommand:
```
khimaira notifier start           # daemon-mode, runs forever
khimaira notifier start --foreground
khimaira notifier stop
khimaira notifier test            # fires a sample notification
```

Pair with `khimaira monitor restart` so both come up together. Optional
systemd / launchd integration via `khimaira notifier install-service`.

## What it does NOT do

- **Auto-respond.** Notifier is read-only. Doesn't try to wake Claude
  sessions programmatically (no clean cross-platform mechanism).
- **Replace the auto-inject hook.** When user types in a session, hook
  still fires. Notifier is for getting their attention to type at all.

## Implementation steps

1. **SSE endpoint in khimaira-monitor** — `/api/sessions/notifications/stream`
   - Subscribes internally to file watchers on inbox.jsonl + handoffs.jsonl
   - Emits one SSE event per new write
2. **Notifier daemon** in `packages/khimaira/src/khimaira/notifier/` (or its
   own subpackage)
   - Subprocess, polls SSE, fires libnotify/osascript/win
3. **CLI** — `khimaira notifier {start,stop,test,install-service}`
4. **Config loader** — TOML at `~/.config/khimaira/notifier.toml`
5. **Tests** — mock SSE source, verify notification calls (mock the OS
   wrapper; don't actually fire real notifications during CI)

## Effort

~140 LOC + tests = **~half-day to MVP** (Linux only), full day for
cross-platform polish.

## Risks

1. **OS notification permissions.** macOS prompts for permission on first
   notification; user has to allow. Document this in setup.
2. **Notification fatigue.** Default config should be aggressive about
   filtering routine `inbox_note` events — only `incoming_question` and
   `handoff` are noisy enough to warrant a tray ping out-of-the-box.
3. **Quiet hours timezone drift.** Use system TZ, not UTC. Easy to get
   wrong.

## Open decisions

1. **Should the notifier track which sessions YOU have open?** Otherwise
   it'll fire notifications for sessions you've forgotten about. Could
   auto-discover via `claude code` process scan or require explicit
   registration.
2. **Should notifications include an action button** ("View") that
   opens the appropriate URL in khimaira-monitor? GTK notifications
   support this; macOS NSUserNotification too. Adds polish.
3. **Aggregation window.** 5 messages in 30 seconds = 5 notifications
   or 1 grouped one? Lean: group at >3 within 60s.
