# khimaira watchers

Daemon-side polling scripts that surface time-sensitive signals to active
sessions via the daemon's inbox + PushNotification surfaces. Wired up
as systemd user timers — no Claude Code session required for them to run.

## Scripts

### `khimaira-bottleneck-watch.sh` (v1.7)

Detects master-as-bottleneck conditions and escalates through three tiers:

- **T1 — Suggest** (cooldown-gated, default 60 min): notify-send +
  `session_post_notice` fan-out to every session active in the last 30 min.
  The in-session notice surfaces in each session's `UserPromptSubmit`
  hook context on next turn, giving the user copy-pasteable `/model sonnet`,
  `/effort medium`, and `/khimaira-deputize <vice>` commands in the window
  where they're already working. **This is the load-bearing surface** —
  desktop notifications alone aren't reliable.

- **T2 — Auto-deputize** (fires when bottleneck persists ≥ 15 min):
  fires `chat_transfer_membership(..., as_deputize=true)` directly via
  daemon HTTP for each chat the master is creator of. Bypasses the
  rate-limited master entirely. Opt-out via `KHIMAIRA_AUTO_DEPUTIZE=0`
  in the service env.

- **T3 — Clear**: state files removed; full cycle resets on next clean
  poll. No-op when bottleneck never fired.

#### Heuristic for "bottleneck"

`awaiting_count ≥ 2 AND master_stale` where:

- `awaiting_count`: sessions in `awaiting-review` status with
  `last_active > 30 min`.
- `master_stale`: at least one `orchestrating` session whose most-recent
  `session_log_decision` is older than 20 min.

Tuning knobs at the top of the script: `THRESHOLD_MIN`,
`DECISION_STALE_MIN`, `MIN_BOTTLENECKED`, `NOTIFY_COOLDOWN_MIN`,
`AUTO_DEPUTIZE_AFTER_MIN`, `DEPUTIZE_COOLDOWN_MIN`.

#### State files

Under `$XDG_STATE_HOME/khimaira/` (default `~/.local/state/khimaira/`):

- `bottleneck-watch.last-alert`   — T1 notify cooldown timestamp.
- `bottleneck-watch.first-seen`   — T1 first-detection timestamp (drives T2 trigger).
- `bottleneck-watch.last-deputize` — T2 cooldown timestamp.
- `bottleneck-watch.log`           — append-only log of every invocation.

## Install

```bash
# 1. Copy scripts to local bin
cp khimaira-bottleneck-watch.sh ~/.local/bin/
chmod +x ~/.local/bin/khimaira-bottleneck-watch.sh

# 2. Install systemd user unit + timer
mkdir -p ~/.config/systemd/user
cp khimaira-bottleneck-watch.service ~/.config/systemd/user/
cp khimaira-bottleneck-watch.timer ~/.config/systemd/user/

# 3. Enable + start
systemctl --user daemon-reload
systemctl --user enable --now khimaira-bottleneck-watch.timer

# Verify
systemctl --user list-timers khimaira-bottleneck-watch.timer
```

## Disable

```bash
# Stop firing
systemctl --user disable --now khimaira-bottleneck-watch.timer

# Reset state (optional)
rm ~/.local/state/khimaira/bottleneck-watch.*
```

## Why daemon-side?

The watcher runs independently of any Claude Code session. When the
master itself is rate-limited (the very condition this watcher fires
on), the master cannot invoke `/khimaira-deputize` or any other slash
command because it has no remaining capacity. T2's auto-deputize fires
via daemon HTTP, bypassing the master entirely — closing the
chicken-and-egg gap that v1.6's recommendation primitive left open.

Per the gap-typology framework (see
`tasks/khimaira-chat/PHASE-B-V2-ROLES-AUDIT.md` postscript): v1.7
extends v1.6's just-in-time recommendation primitive with a daemon-
side autonomic-recovery layer for the case where the master can't
self-recover. The recommendation layer (v1.6 slash commands, v1.5
directive emit, v1.6.1 SessionStart reminder) handles the common case;
the autonomic layer (this watcher's T2) handles the failure mode.
