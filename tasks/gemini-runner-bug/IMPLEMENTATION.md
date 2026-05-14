# Gemini runner — prompt-passing broken via `-p` flag

**Status**: identified 2026-05-13, workaround landed, fix pending
**Severity**: medium — auto-router silently degrades when gemini is in the pool
**Owner**: TBD

## Symptom

Every call to the gemini runner returns an empty / generic response
regardless of prompt content, with `0→0 tokens`:

```bash
$ mcp__khimaira__delegate(prompt="Reply with: pong", tier="flash")
→ "_(via gemini/gemini-2.5-flash · 0→0 tokens · 12.2s · mode=explicit-tier)_

Hello! I'm ready for your instructions. What can I help you with?"
```

That generic response is what gemini emits when it gets NO prompt
content — meaning our prompt isn't reaching the model.

The 12-second latency on a "tell me your fallback message" reply is
itself a clue: the CLI is doing a full session-init handshake without
ever receiving the prompt.

## Repro

Any call to `mcp__khimaira__delegate(prompt=<anything>, tier="flash")`
or `mcp__khimaira__auto(prompt=...)` when the auto-router picks
gemini. Confirmed 2026-05-13 via direct probe + via scribe's
summarize/extract nodes returning empty results.

Equivalent for `tier="haiku"` (claude) returns correct content with
real token counts — so the framework's dispatch path works; the bug
is gemini-specific.

## Root cause hypothesis

The runner builds the gemini CLI invocation as:

```python
# packages/khimaira/src/khimaira/dispatch/runners/gemini.py:61
cmd = [
    self.cmd,
    "--skip-trust",
    "--approval-mode", "plan",
    "-m", model_id,
    "-p", prompt,         # ← suspect
    "-o", "json",
]
```

The `-p prompt` arg passing assumes the CLI accepts arbitrarily large
prompts via a single argv entry. Two possibilities:

1. **gemini CLI silently truncates `-p` past a limit** — common for
   shell-arg pipelines. Multi-line prompts with quotes / special
   chars get especially mangled.
2. **`-p` expects a file path, not literal text** in newer gemini CLI
   versions. The CLI may parse the arg as a filename, fail to find
   it, and silently fall back to interactive mode (which produces the
   generic welcome reply).

The Anthropic claude CLI (in `claude.py`) uses stdin for the prompt,
which is the safer pattern. Likely the gemini runner needs the same
treatment.

## Workaround (in place)

Scribe's summarize/extract nodes pinned to `tier="haiku"` (claude) so
the meeting pipeline works without depending on the gemini runner.
See `packages/scribe/src/scribe/nodes/summarize.py` + `extract.py`.

Cost impact: minimal. Claude haiku is $0.8/M input; gemini-flash is
$0.075/M (~10x cheaper) — but the scribe summarize/extract prompts
are small (~3-10K tokens) and run once per meeting. Marginal cost
difference is < $0.001 per standup.

Other auto-router consumers degrade silently when the router picks
gemini. Until this is fixed, users can workaround by setting
`enabled_for_auto: false` on the gemini entries in
`~/.khimaira/models.yaml`. `khimaira models sync` will surface this
as a diff if shipped defaults re-enable them.

## Fix plan

1. **Empirical probe** — drop the existing `cmd` shape into a
   subprocess directly with a known prompt, observe stdout, stderr,
   exit code. Compare to a stdin-piped variant.
2. **Read gemini CLI's docs / source** — `gemini --help` and
   `gemini -p --help` to confirm the actual `-p` semantics in the
   currently-installed CLI version.
3. **Refactor the runner** to use stdin (mirror claude.py's pattern):

   ```python
   proc = await asyncio.create_subprocess_exec(
       self.cmd, "--approval-mode", "plan", "-m", model_id, "-o", "json",
       stdin=asyncio.subprocess.PIPE,
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE,
       env=_build_subprocess_env(),
   )
   stdout, stderr = await proc.communicate(input=prompt.encode())
   ```

4. **Re-test** via `mcp__khimaira__delegate(tier="flash", prompt="...")`
   — expect real token counts + correct response text.
5. **Revert the scribe workaround** — change `tier="haiku"` back to
   `tier="auto"` in summarize.py + extract.py. Update the test
   assertions to match.
6. **Smoke test scribe end-to-end** — call `scribe_summarize` on a
   real transcript; verify the resulting summary is non-empty.

## Tests required after fix

- New unit test in `packages/khimaira/tests/test_gemini_runner.py`:
  mock asyncio.create_subprocess_exec, verify the prompt arrives via
  stdin and the parsed RunnerResult has the right shape.
- Existing `tests/test_delegate_auto_e2e.py` integration tests should
  start passing for the gemini path (currently skip when only
  claude+gemini are installed but no ollama).

## Why this is filed as a separate task

Surfaced during the 2026-05-13 scribe-integration smoke test. Scribe
is correctly wired and works end-to-end with the haiku workaround;
the gemini bug pre-exists scribe entirely and would have been hit by
ANY auto-routed call that happened to land on gemini. Fixing it
properly deserves its own focused work block, not a scope creep into
the scribe integration.
