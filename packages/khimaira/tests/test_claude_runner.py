"""Unit tests for ClaudeRunner._parse 3-way classification (#13a).

Acceptance criteria:
  AC-1: "Server is temporarily limiting requests (not your usage limit)" → ClaudeTransientError
  AC-2: "credit balance too low" → ClaudeAuthError (no retry, unchanged)
  AC-3: "overloaded_error" → ClaudeTransientError
  AC-4: generic "rate limit" with no account context → ClaudeTransientError (removed from hard-stop)
  AC-5: existing account-hard-stop tests still pass
"""

from __future__ import annotations

import json
import pytest


def _make_runner():
    from khimaira.dispatch.runners.claude import ClaudeRunner

    return ClaudeRunner()


def _error_json(result_text: str) -> str:
    return json.dumps({"is_error": True, "result": result_text})


# ---------------------------------------------------------------------------
# AC-1: Joseph's exact post-exhaustion string → ClaudeTransientError
# ---------------------------------------------------------------------------


def test_transient_joseph_exact_string():
    """'Server is temporarily limiting requests (not your usage limit)' → transient."""
    from khimaira.dispatch.runners.claude import ClaudeTransientError

    runner = _make_runner()
    with pytest.raises(ClaudeTransientError):
        runner._parse(
            _error_json("Server is temporarily limiting requests (not your usage limit)"),
            latency=0.1,
            model_id="test",
        )


# ---------------------------------------------------------------------------
# AC-2: Account-level hard stop → ClaudeAuthError
# ---------------------------------------------------------------------------


def test_account_credit_balance_too_low():
    """'credit balance too low' → ClaudeAuthError (no retry)."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError

    runner = _make_runner()
    with pytest.raises(ClaudeAuthError):
        runner._parse(
            _error_json("Your credit balance is too low to make requests."),
            latency=0.1,
            model_id="test",
        )


def test_account_invalid_api_key():
    """'invalid api key' → ClaudeAuthError."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError

    runner = _make_runner()
    with pytest.raises(ClaudeAuthError):
        runner._parse(
            _error_json("invalid api key provided"),
            latency=0.1,
            model_id="test",
        )


def test_account_authentication_error():
    """'authentication' → ClaudeAuthError."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError

    runner = _make_runner()
    with pytest.raises(ClaudeAuthError):
        runner._parse(
            _error_json("authentication failed — check your API key"),
            latency=0.1,
            model_id="test",
        )


def test_account_usage_limit():
    """'usage limit' (account cap) → ClaudeAuthError, NOT transient."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError

    runner = _make_runner()
    with pytest.raises(ClaudeAuthError):
        runner._parse(
            _error_json("You have exceeded your usage limit for this period."),
            latency=0.1,
            model_id="test",
        )


# ---------------------------------------------------------------------------
# AC-3: overloaded_error / overloaded → ClaudeTransientError
# ---------------------------------------------------------------------------


def test_transient_overloaded_error():
    """'overloaded_error' → ClaudeTransientError."""
    from khimaira.dispatch.runners.claude import ClaudeTransientError

    runner = _make_runner()
    with pytest.raises(ClaudeTransientError):
        runner._parse(
            _error_json("overloaded_error: the API is currently overloaded"),
            latency=0.1,
            model_id="test",
        )


def test_transient_overloaded():
    """'overloaded' → ClaudeTransientError."""
    from khimaira.dispatch.runners.claude import ClaudeTransientError

    runner = _make_runner()
    with pytest.raises(ClaudeTransientError):
        runner._parse(
            _error_json("API is currently overloaded, please try again"),
            latency=0.1,
            model_id="test",
        )


# ---------------------------------------------------------------------------
# AC-4: generic "rate limit" no longer a hard stop — now transient
# ---------------------------------------------------------------------------


def test_rate_limit_no_account_context_is_transient():
    """Generic rate limit strings removed from hard-stop; now transient."""
    from khimaira.dispatch.runners.claude import ClaudeTransientError

    runner = _make_runner()
    # "rate_limit" was previously hard-stop; now transient (the conflation fix)
    with pytest.raises(ClaudeTransientError):
        runner._parse(
            _error_json("overloaded — rate limit exceeded on server side"),
            latency=0.1,
            model_id="test",
        )


# ---------------------------------------------------------------------------
# AC-5: non-error response succeeds (regression guard)
# ---------------------------------------------------------------------------


def test_success_response_returns_runner_result():
    """Normal non-error response still parses correctly."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError, ClaudeTransientError

    runner = _make_runner()
    payload = json.dumps({
        "is_error": False,
        "result": "Task completed successfully.",
        "session_id": "test-session",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    result = runner._parse(payload, latency=0.5, model_id="claude-opus-4-7")
    assert result.text == "Task completed successfully."
    assert result.latency_s == pytest.approx(0.5)


def test_billing_error_is_hard_stop_not_transient():
    """'billing' → ClaudeAuthError, confirming precedence of account-check."""
    from khimaira.dispatch.runners.claude import ClaudeAuthError, ClaudeTransientError

    runner = _make_runner()
    with pytest.raises(ClaudeAuthError):
        runner._parse(
            _error_json("billing issue — your payment method has been declined"),
            latency=0.1,
            model_id="test",
        )
