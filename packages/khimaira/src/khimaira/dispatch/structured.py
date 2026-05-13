"""run_structured() — get a Pydantic-validated object out of a CLI runner.

The hard problem with pure-CLI substrate: APIs (Anthropic, OpenAI) support
tool-use / strict-JSON modes that GUARANTEE the response parses against a
schema. CLIs don't (mostly). They emit free-form text.

This helper bridges the gap. It:
  1. Wraps the user prompt with a JSON-schema instruction telling the model
     EXACTLY what shape to return.
  2. Invokes the runner.
  3. Parses the response, with sensible "extract JSON from prose" recovery
     for runners that occasionally include preamble.
  4. Validates against the Pydantic schema.
  5. On parse/validation failure, retries up to N times with the original
     output included as feedback.

The escalate-to-bigger-model fallback is one layer up — `dispatch.escalation`
wraps run_structured and tries a more capable runner if quality fails.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from khimaira.log import get_logger

from .runners import RunnerResult, get_runner

log = get_logger("dispatch.structured")

T = TypeVar("T", bound=BaseModel)


class StructuredCallError(Exception):
    """run_structured() exhausted retries without producing a valid object."""

    def __init__(self, message: str, last_output: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.last_output = last_output
        self.attempts = attempts


_PROMPT_TEMPLATE = """{user_prompt}

---

You MUST respond with a single JSON object that exactly matches this schema:

```json
{schema_json}
```

Required:
- Output ONLY the JSON object. No prose before or after. No code-fence labels.
- Every required field must be present.
- String enum fields must use one of the listed allowed values exactly.
- Numbers must be numbers (not strings).

Begin your response with `{{` and end with `}}`."""


_RETRY_TEMPLATE = """Your previous response could not be parsed. Error:

{error}

Your previous output was:

{prior_output}

Try again. Respond with ONLY a single valid JSON object matching the schema:

```json
{schema_json}
```

No prose. No code fences. Begin with `{{` and end with `}}`."""


# Strip ```json ... ``` fences and find the outermost JSON object.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(raw: str) -> str:
    """Recover a JSON object from prose-contaminated output. Best-effort.

    Strategy:
      1. Look for a ```json fence
      2. Otherwise grab from the first `{` to the matching closing `}`
      3. If both fail, return the raw string and let json.loads raise
    """
    if not raw:
        return raw

    # Code-fence path
    m = _JSON_FENCE_RE.search(raw)
    if m:
        return m.group(1)

    # Naive bracket-balance — finds the outermost JSON object
    start = raw.find("{")
    if start < 0:
        return raw
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return raw[start:]


async def run_structured(
    runner_name: str,
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    max_retries: int = 2,
    **kwargs: object,
) -> tuple[T, RunnerResult]:
    """Run a prompt and parse the response into a Pydantic schema.

    Returns (parsed_object, runner_result). The RunnerResult is included so
    callers can record token usage / latency without making a second call.

    Raises:
        StructuredCallError: when retries are exhausted.
    """
    runner = get_runner(runner_name)
    schema_json = json.dumps(schema.model_json_schema(), indent=2)

    full_prompt = _PROMPT_TEMPLATE.format(
        user_prompt=prompt, schema_json=schema_json,
    )
    last_error: str = ""
    last_output: str = ""

    for attempt in range(max_retries + 1):
        if attempt == 0:
            current_prompt = full_prompt
        else:
            log.info(
                "run_structured: retry %d/%d on %s — last error: %s",
                attempt, max_retries, runner_name, last_error[:100],
            )
            current_prompt = _RETRY_TEMPLATE.format(
                error=last_error,
                prior_output=last_output[:2000],
                schema_json=schema_json,
            )

        result = await runner.run(
            current_prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            **kwargs,
        )
        last_output = result.text

        try:
            extracted = _extract_json(result.text)
            data = json.loads(extracted)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = f"JSON parse error: {e}"
            continue

        try:
            obj = schema.model_validate(data)
            return obj, result
        except ValidationError as e:
            last_error = f"Schema validation error: {e}"
            continue

    raise StructuredCallError(
        f"run_structured: exhausted {max_retries + 1} attempts on runner={runner_name}, "
        f"schema={schema.__name__}. Last error: {last_error}",
        last_output=last_output,
        attempts=max_retries + 1,
    )
