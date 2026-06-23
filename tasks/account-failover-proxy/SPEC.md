# Account failover at the concurrency-proxy — switch to a backup account on limit

> Status: SPEC / idea · 2026-06-23 · filed by khimaira-0. Build when the weekly limit
> is stable + a backup account exists. Answer to Joseph's "if I get a backup account,
> can it auto-switch when we hit the limit?"

## Goal

When the primary Anthropic account hits its weekly usage cap, roster sessions (Claude
Code) automatically fail over to a backup account and keep working, then switch back
when the primary's limit resets — with no manual re-auth per window.

## Why the concurrency-proxy is the right seam

Roster sessions already route ALL their Anthropic traffic through the local
concurrency-proxy (port 8741, systemd — see [[project_khimaira_concurrency_proxy]];
it absorbs the 32-session throttle). So the proxy already sees every request + every
response, including the limit/usage-cap errors. That makes it the natural failover
point: detect the cap on responses, swap the upstream credential, retry — transparent
to every Claude Code window. No per-window change needed.

## Design

1. Proxy holds credentials for account A (primary) + account B (backup).
2. **Detect the cap:** key on the usage-limit response (the 429 / "weekly limit"
   /usage-cap signal CC surfaces). On that, mark A `limited_until=<reset_ts>` (parse the
   reset from the response if present, else a conservative default).
3. **Fail over:** rewrite the auth on the outbound request to account B's credential,
   retry the same request. All subsequent requests route to B until A's reset.
4. **Switch back:** once `now > A.limited_until` (or A returns non-capped again on a
   probe), revert to A. Log every switch.
5. Expose state (`/health` or a status line): which account is active, A's reset ETA.

## The credential-swap detail (the load-bearing part to verify first)

CC auth is OAuth (the subscription), NOT an API key — the roster runs on OAuth
([[feedback_apikey_empty_not_oauth]]). Two backup shapes:
- **Backup = 2nd subscription (2nd OAuth):** proxy swaps the `Authorization: Bearer
  <oauthA>` header for `<oauthB>`. Needs B's OAuth token captured via a separate
  `claude` login, stored for the proxy. Same weekly-cap model, just a 2nd bucket.
- **Backup = Console API key (pay-per-token):** proxy swaps `Authorization: Bearer
  <oauth>` for `x-api-key: <key>` (+ drops the OAuth header). NO weekly cap (credits,
  pay-per-token) → effectively unlimited failover headroom, but it BILLS per token.
  Cleaner failover; watch spend. ⚠️ the apiKeyHelper-empty footgun
  ([[feedback_apikey_empty_not_oauth]]) — inject the key proxy-side, don't rely on CC's
  apiKeyHelper.

**Verify FIRST (throwaway session):** that the proxy can actually override CC's auth
header end-to-end (CC may pin/refresh its own OAuth). If CC re-asserts its token after
the proxy, the swap has to happen at a layer CC can't override — confirm the mechanism
before building (this is the [[feedback_apikey_empty_not_oauth]] / verify-foundational-
auth-in-a-throwaway discipline).

## Risks / open

- OAuth-B token lifecycle: it expires/refreshes; the proxy needs a fresh B token (a
  background `claude` re-auth for B, or a stored long-lived credential).
- Mid-stream failover: a request already streaming when A caps — fail over on the NEXT
  request, don't try to resume a streamed one.
- Per-session vs global: simplest is global (all windows switch together); per-session
  is possible but adds state.

## Cross-references
- [[project_khimaira_concurrency_proxy]] — the proxy this extends (8741, systemd).
- [[feedback_apikey_empty_not_oauth]] — auth mechanism + the apiKeyHelper footgun;
  verify the credential swap in a throwaway session before building.
