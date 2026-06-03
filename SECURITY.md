# Security model

## Same-uid trust boundary (TM1 daemon-auth)

The khimaira monitor daemon binds exclusively to `127.0.0.1`, which closes the
remote-attacker threat surface. Against a **deliberate same-uid local process**
(e.g. another process running as the same Unix user), the daemon does not provide
airtight isolation. Airtight defense would require OS-level isolation (separate
Unix uids, `SO_PEERCRED` socket credential passing) — disproportionate for a
single-operator localhost development tool.

### What TM1 closes

**Accidental cross-session authority claims** — the realistic failure mode for a
multi-session orchestration tool. When an honest MCP subprocess sends
`X-Session-ID` sourced from its own `CLAUDE_CODE_SESSION_ID` environment variable
(set by Claude Code at launch), it is structurally incapable of claiming another
session's identity: the env var is its own, correct by construction. TM1 wires
this signal into all privilege-granting daemon endpoints; a confused session
literally cannot self-grant another's role without explicitly forging the header.

### What TM1 does NOT close

A **deliberate** same-uid process that manually forges the `X-Session-ID` header
with a victim's UUID can still impersonate that session. Session UUIDs are not
secret — they are broadcast in `sender_id` fields in chat history. This is the
accepted residual threat under the single-operator model. Closing it requires
TM2 (per-session secret minted at registration, delivered via a 0600 session-dir
file) or OS isolation — both declined as disproportionate; the same-uid process
could read the secret file or bypass the daemon entirely via direct filesystem
access anyway.

### Rollout (Phase B)

**B.1 (current):** `require_actor` dependency on all privilege endpoints reads
`X-Session-ID` and logs a WARN when absent, falling back to the body
`by_session_id` field. Zero breakage for sessions running old MCP code (no
header). The khimaira-chat MCP client now sends the header via
`_caller_headers()` / `_request_with_retry` — new and restarted sessions use it
automatically.

**B.2 flip-trigger:** when the `daemon-auth B.1` WARN rate reaches **zero**
in the daemon log (observable signal that every live session has cycled onto
header-sending code), flip `require_actor` to hard-reject absent headers
(HTTP 401: "X-Session-ID required; restart your session to pick up the auth
header"). The self-explaining 401 makes any idle session that missed the
rollout self-healing — clear error → restart → fixed.
