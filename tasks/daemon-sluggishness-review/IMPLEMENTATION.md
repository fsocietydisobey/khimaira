# khimaira-monitor Daemon — Sluggishness Review

> **Status:** OPEN. Flagged by Joseph 2026-06-04: **janice (jp master) keeps reporting
> the daemon feels sluggish.** Repeated reports = a real signal, not a one-off. Needs a
> proper performance investigation next session. Written at wind-down.

## Symptom

The `khimaira-monitor` daemon (port 8740 — serves all session/chat/SSE/handoff/notice
ops for **~32 sessions across 2 rosters**) is reportedly slow. Anecdotal but
**recurring from janice specifically** (jp master, the heaviest-traffic session).
Observed corroboration this session: `chat_task_update → approved` **timed out** twice
(the action landed, the response didn't) — consistent with daemon latency under load.

## Why it's plausible (load context)

- **~32 concurrent sessions** all calling `chat_*` / `session_*` tools + holding SSE
  subscriptions → high request + fan-out volume on a single daemon.
- **JSONL read→modify→atomic-rename** is the storage pattern everywhere in
  `monitor/sessions.py` + `monitor/chats.py`. The chat file
  `chat-fdf7c4cbd3bd.jsonl` is at **~2000 messages** and growing — every append /
  `load_room` re-reads the whole file. O(file-size) per op × high volume = the obvious
  suspect.
- **SSE broadcast fan-out** to ~20 members per message (the Family-B `_broadcast` →
  per-member slot-resolve → per-subscriber queue).
- **In-memory heartbeat buffer** + the role-directive / handoff GC paths.

## Hypotheses (rank by likelihood, verify don't assume)

1. **JSONL O(n) re-reads at scale** — `load_room` / session-state reads parse the full
   file per call; at ~2000 msgs × 32 sessions polling, this dominates. → Profile
   `load_room` latency vs file size. Consider an in-memory room cache / tail-index /
   append-only read offset.
2. **Blocking I/O in async handlers** (engineering/performance.md: "no blocking I/O in
   async handlers"). A synchronous file read inside an async route blocks the event
   loop → every other request stalls behind it. → Audit `monitor/api/*.py` routes for
   sync `open()/read()/json.load` on the request path; move to a thread / async.
3. **SSE fan-out cost** — per-broadcast slot-resolution over the registry for every
   member. → Measure broadcast latency at 20 members.
4. **host.docker.internal regression** — I fixed monitor checkpointer host-hammering
   this session (`b3ae727`, `_normalize_monitor_host`). **VERIFY it's still effective
   post-restart** (the daemon restarted several times today) — a re-introduced hammer
   loop would degrade the daemon exactly like this.
5. **Unbounded growth** — chat files / handoffs / role-directives never compacted.
   `gc_role_directives_in_chat` + handoff GC exist; verify they're running and the
   files aren't pathological.

## Investigation steps

1. **Measure, don't guess** (performance.md): time representative ops live —
   `time curl http://127.0.0.1:8740/api/sessions`, a `chat_history`, a
   `session_state`. Establish which endpoints are slow.
2. Check daemon **CPU / memory / event-loop lag** under real roster load
   (`monitor.log`, `top`, any `/health` latency metric).
3. **File sizes**: `du -h ~/.local/state/khimaira/chats/*.jsonl` +
   `~/.local/state/khimaira/sessions/`. Correlate slow ops with large files.
4. Audit the **hot async routes** for blocking I/O (hypothesis 2 — highest-leverage if
   true; a single sync read on a hot path stalls the whole loop).
5. Confirm the **b3ae727 host-fix** still fires (grep `monitor.log` for
   "rewrote checkpointer host"; confirm no `host.docker.internal` connection storms).
6. If JSONL re-reads dominate: design an in-memory room/session cache with invalidation
   on write (the read→modify→rename already serializes writes, so a cache is tractable).

## Connection to today's work

- The **concurrency-proxy** (port 8741) is SEPARATE — it proxies the **Anthropic API**,
  not the daemon. It does **not** touch daemon load. (Don't conflate "API throttle" with
  "daemon sluggish" — different subsystems.)
- Today's daemon restarts deployed the host-fix + Part F + alive-guard; verify none
  regressed latency.

## Acceptance

- Identify the dominant cost (profile-backed, not hypothesized).
- A concrete fix or a measured "it's acceptable, here's the headroom" with numbers.
- If JSONL O(n) is the cause: an in-memory cache or index, with a before/after latency
  measurement at the live file sizes.
