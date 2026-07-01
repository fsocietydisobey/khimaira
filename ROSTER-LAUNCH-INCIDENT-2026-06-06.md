# Roster Launch Incident — 2026-06-06

**Author:** khimaira-0 (session `c5481dee-6d4b-4cef-b961-63b15c70b13a`, the fresh master spawned by
`roster khimaira start --agents 6 --master khimaira`).
**For:** whoever resumes the khimaira master (likely `b401499d`, the prior khimaira-0 with the
27-decision roster-launch-reliability context).
**Status:** roster bootstrap reached "chat created + most members accepted" but is **degraded** — the
`khimaira-chat` MCP server never connected for this master session, so real-time chat is dead and all
chat writes hang. Joseph is terminating this roster and resuming the old khimaira-0.

---

## THE ONE THING TO DO FIRST

**Read `/tmp/khimaira-chat.log`.** It is the unread smoking gun for the whole incident and I never got
to read it (kept getting interrupted). The `khimaira-chat` MCP server is registered (in
`~/.claude.json`) as:

```
khimaira-chat => bash -lc "uv --directory ~/dev/khimaira run khimaira-chat 2>>/tmp/khimaira-chat.log"
```

Its stderr accumulates in `/tmp/khimaira-chat.log`. If `uv run khimaira-chat` is erroring or the entry
point is broken/slow, that log shows it. Also sanity-check the entry point directly:
`timeout 20 uv run khimaira-chat --help` (or `uv run khimaira-chat </dev/null`). The main `khimaira`
MCP server connects fine; only `khimaira-chat` fails — so the fault is specific to that server/entry
point, not the daemon or MCP plumbing generally.

---

## SYMPTOM

- Every roster session reports **"chat registration pending server connect"** (seen in
  `session_state` of `roster-agent` f6f3ebf2 et al.).
- For the master (me): `mcp__khimaira-chat__*` tools **never surfaced** — ~12 `ToolSearch` attempts
  across the session, the server stayed "still connecting" the entire time. I could not call
  `chat_my_chats` (no SSE subscriber → master is deaf to real-time chat) nor any chat write tool.
- Driving the chat over the daemon REST API works for **GET** but every **POST hangs ~15s on the
  response** (it commits server-side, but the response blocks — almost certainly the SSE
  role-directive fan-out). So even the REST fallback is not viable for live orchestration.

## WHAT IS CONFIRMED HEALTHY (so DON'T chase these)

- **Daemon (8740):** `/health` → 200. `GET /api/chats?session_id=...` → 200.
- **Concurrency-proxy (8741):** `/health` → 200, listening under the `khimaira` process.
- **This master was launched correctly through the proxy:** my env has
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8741` **and** `ENABLE_TOOL_SEARCH=1`. So the
  "missing `KHIMAIRA_PROXY_URL` on the command line" theory is a **red herring** — `bin/roster`
  auto-routes through the proxy when 8741 `/health` answers and sets `ENABLE_TOOL_SEARCH=1`
  alongside `ANTHROPIC_BASE_URL` (roster:319-324, 443-450). The proxy path *was* taken.
- **All 14 windows physically launched** (kitty ids): 786 khimaira-0 (master/me), 787 analyst-1,
  788 architect-1, 789 verifier-1, 790 intake-1, 791 agent-1, 792 agent-2, 793 agent-3,
  794 agent-4, 795 agent-5, 796 agent-6, 797 critic-1, 798 observer-1, 799 tracker-1.
  The windows are NOT the problem — the in-session `khimaira-chat` MCP connect is.

## ROOT-CAUSE CANDIDATES (ranked)

1. **`khimaira-chat` MCP entry point failing or too slow on boot** (MOST LIKELY). 14 sessions each
   spawn `uv run khimaira-chat` + `uv run khimaira mcp` simultaneously = ~28 `uv` resolves hammering
   disk/CPU during the boot storm. If `uv run khimaira-chat` is slow or errors, Claude Code leaves it
   "still connecting" indefinitely. **Confirm via `/tmp/khimaira-chat.log`.** Possible fixes:
   pre-warm/pre-resolve the `uv` env, pace MCP-server startup, or fix a broken `khimaira-chat`
   console-script entry point.
2. **A regression in the `khimaira-chat` server code itself** (crash-on-start). Same log confirms.
3. (Ruled down) proxy / tool-search env — confirmed correctly set for the master; not the cause.

## FINDINGS IN `bin/roster` (real bugs, worth fixing regardless)

1. **No `stop`/`down` mode** — only `start` and `resume`. Terminating a roster requires manually
   closing kitty windows + reaping daemon records. Consider adding `roster <p> stop`.
2. **Pre-flight reaper can't scope khimaira worker names (answers "shouldn't start have deleted
   previous sessions?").** The reaper (roster:223-288) deletes records where
   `name == master OR name startswith PREFIX`. The **khimaira roster runs prefix-less** (`PREFIX=""`),
   so it only matches the master name `khimaira-0` — worker names (`agent-1`, `intake-1`, …) are
   never reaped → the 94-session registry buildup + duplicate-name entanglement. The `jp` roster
   (prefix `jp-`) reaps fine. **Fix:** give khimaira a prefix, or have the reaper also scope by the
   known role-name set when PREFIX is empty.
3. **The launcher self-closes its own window** (roster:985,
   `kitty @ close-window --match id:${KITTY_WINDOW_ID}`). Implication for the master: you must NEVER
   run `roster` from the master's own shell — `KITTY_WINDOW_ID` would be the master's, and it would
   close the master. Always relaunch from a throwaway window (or `kitty @ launch` a dedicated
   launcher window).

## CURRENT STATE AT HANDOFF

- **Chat created despite the hang:** `chat-0f3f884de855` "khimaira roster — 2026-06-06",
  topology hierarchical, roles correctly bound for all 10 (master + intake + 4 agents I could resolve
  + observer + architect + critic + tracker). 8/9 workers `accepted`; `842b31c8` (registered as
  "khimaira-agent") was `pending`. **This chat will be stale once the roster is terminated — recreate
  fresh on the resumed roster.**
- **Intended roster = 14:** master + 6 agents + intake + observer + architect + critic + analyst +
  verifier + tracker. (`--agents 6` → 6 agents; the rest are the fixed support roster.)
- **Registry is polluted:** `session_list` shows 94 sessions, many stale duplicate-named
  (`intake-1`, `agent-2`, `architect-1`, `tracker-1` across days). Name-based role resolution is
  hazardous until GC'd. Use `session_delete(force=true)` or `DELETE /api/sessions/{sid}?force=true&reap=true`.

## RECOMMENDED NEXT ACTIONS (for the resumed master)

1. Read `/tmp/khimaira-chat.log` + test `uv run khimaira-chat` → identify why the MCP server won't
   connect. **Single-session repro**: launch ONE throwaway `claude-chat` window through the proxy and
   watch whether `khimaira-chat` connects — do NOT thrash the full 14-window roster to debug (matches
   the prior master's proven method).
2. Fix the root cause (likely entry-point/boot-pacing).
3. GC the stale session registry.
4. Consider the two `bin/roster` bugs above (prefix-scoped reaper; add a `stop` mode).
5. Only then relaunch the full roster + `/khimaira-bootstrap-roster`.
