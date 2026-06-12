"""Final #18 lead: FORK + LIVE SSE subscribers connecting AT STARTUP.

Forks a real khimaira monitor daemon (double-fork/setsid/no-TTY, real copied
state, :8798) then immediately storms ~20 SSE clients that retry-connect from
t=0 — so they flood the daemon with concurrent EventSourceResponse setup right
as it boots, racing auto_dispatch's FIRST uvloop sleep timer (the prod
condition: ~27 chat MCP clients re-subscribe seconds after a bounce).

Signal = AD-WOKE (WARNING) in the daemon log:
  AD-WOKE never fires → REPRODUCED (boot SSE flood starves the first sleep timer)
  AD-WOKE fires       → last lead eliminated → PARK.
"""

import asyncio
import os
import time

R = "/tmp/ad_fork_real"
os.environ["XDG_STATE_HOME"] = f"{R}/state"
os.environ["XDG_DATA_HOME"] = f"{R}/data"
os.environ["XDG_CONFIG_HOME"] = f"{R}/config"
os.environ["XDG_CACHE_HOME"] = f"{R}/cache"
os.environ["KHIMAIRA_DEBUG_CANARY"] = "1"
os.environ["KHIMAIRA_AUTO_DISPATCH_S"] = "5"
os.environ["KHIMAIRA_ROSTER_WATCH_S"] = "5"
os.environ.pop("KITTY_LISTEN_ON", None)  # safety: no kitty inject
os.environ.pop("KITTY_WINDOW_ID", None)

PORT = 8798
N_SSE = 22
HOLD_S = 34
SIDS = [
    "42517fcd-f970-4fd9-87ed-223a0d6084c8",
    "136ce9b9-1c68-4cb8-92fd-64abc6c0c91c",
    "56be088d-943f-49aa-8a69-7f053ce976c4",
    "664a05f4-6a78-494f-97b4-349fe900532c",
    "7ce2badc-bdc6-4b93-aa58-88d74775b11b",
    "b401499d-fece-46c4-995e-b40eadfa5d89",
]

from khimaira.monitor.daemon import daemonize_and_serve  # noqa: E402

pid = daemonize_and_serve(port=PORT)
print(f"FORKED DAEMON PID={pid} port={PORT} — storming {N_SSE} SSE clients from boot...",
      flush=True)


_connected = set()
_first_connect_t = [None]


async def client(i, deadline):
    import httpx

    sid = SIDS[i % len(SIDS)]
    url = f"http://127.0.0.1:{PORT}/api/chats/events?session_id={sid}"
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code == 200:
                        if _first_connect_t[0] is None:
                            _first_connect_t[0] = time.time()
                        _connected.add(i)
                    async for _line in r.aiter_lines():
                        if time.time() > deadline:
                            return
        except Exception:
            await asyncio.sleep(0.04)  # port not up yet / dropped → retry hard


async def main():
    t0 = time.time()
    deadline = t0 + HOLD_S
    await asyncio.gather(*[client(i, deadline) for i in range(N_SSE)])
    ft = _first_connect_t[0]
    print(
        f"SSE VERIFY: {len(_connected)}/{N_SSE} clients connected (HTTP 200); "
        f"first connect at +{(ft - t0):.1f}s after fork"
        if ft else f"SSE VERIFY: 0/{N_SSE} connected (NONE reached the daemon)",
        flush=True,
    )


asyncio.run(main())
print("SSE storm done", flush=True)
