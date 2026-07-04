"""Tests for khimaira.monitor.notebook_redaction — the deterministic secret
detector for sensitive notes.

This is a security boundary: every test here asserts that a SPECIFIC secret
VALUE is absent from the redacted output, not just that "some redaction
happened" — the whole point is that the real value never survives.
"""

from __future__ import annotations

from khimaira.monitor import notebook_redaction as redaction


def test_redact_secrets_no_op_when_nothing_detected():
    text = "Just a normal note about widgets and their configuration."
    llm_text, redactions = redaction.redact_secrets(text)
    assert llm_text == text
    assert redactions == []


def test_redact_secrets_anthropic_key():
    secret = "sk-ant-api03-" + "a" * 40
    text = f"My key is {secret} — keep it safe."
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "anthropic_key" for r in redactions)
    assert not any(secret in r["placeholder"] for r in redactions)  # never the value


def test_redact_secrets_openai_key():
    secret = "sk-proj-" + "b" * 40
    text = f"OPENAI_API_KEY={secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text


def test_redact_secrets_aws_key_id():
    secret = "AKIAIOSFODNN7EXAMPLE"
    text = f"aws_access_key_id = {secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    # The variable name also matches the generic ACCESS_KEY assignment
    # pattern (higher priority) — either label is a correct redaction; the
    # security property (the value is gone) is what matters, not which
    # pattern claimed it.
    assert any(r["kind"] in ("aws_key_id", "assignment_secret") for r in redactions)


def test_redact_secrets_aws_key_id_standalone_not_in_assignment_context():
    """Without an assignment context, the prefixed-token pattern alone must
    still catch it."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    text = f"the access key is {secret}, save it somewhere safe"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "aws_key_id" for r in redactions)


def test_redact_secrets_google_api_key():
    secret = "AIza" + "c" * 35
    text = f"const key = '{secret}';"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "google_api_key" for r in redactions)


def test_redact_secrets_github_token():
    secret = "ghp_" + "d" * 36
    text = f"GITHUB_TOKEN={secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text


def test_redact_secrets_slack_token():
    secret = "xoxb-" + "1234567890-" + "abcdefghijklmnop"
    text = f"Slack bot token: {secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "slack_token" for r in redactions)


def test_redact_secrets_gitlab_token():
    secret = "glpat-" + "e" * 20
    text = f"GITLAB_TOKEN={secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text


def test_redact_secrets_bearer_token():
    secret = "abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"Authorization: Bearer {secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "bearer_token" for r in redactions)


def test_redact_secrets_assignment_password():
    secret = "sup3rSecr3tPassw0rd!!"
    text = f"DB_PASSWORD={secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "assignment_secret" for r in redactions)


def test_redact_secrets_assignment_quoted_value():
    secret = "sup3rSecr3tPassw0rd!!"
    text = f'DB_PASSWORD="{secret}"'
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert '"' in llm_text  # quotes themselves are preserved, only the value masked


def test_redact_secrets_export_style_assignment():
    secret = "sup3rSecr3tPassw0rd!!"
    text = f"export API_TOKEN={secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text


def test_redact_secrets_pem_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = f"Here is the key:\n{pem}\nThat's it."
    llm_text, redactions = redaction.redact_secrets(text)
    assert "MIIEowIBAAKCAQEA" not in llm_text
    assert any(r["kind"] == "pem_private_key" for r in redactions)
    assert "Here is the key:" in llm_text  # surrounding prose untouched
    assert "That's it." in llm_text


def test_redact_secrets_connection_string_password():
    text = "DATABASE_URL=postgres://admin:sup3rSecr3tPass@db.example.com:5432/mydb"
    llm_text, redactions = redaction.redact_secrets(text)
    assert "sup3rSecr3tPass" not in llm_text
    assert "admin" in llm_text  # username is not a secret
    assert "db.example.com" in llm_text  # host is not a secret
    assert any(r["kind"] == "connection_string_password" for r in redactions)


def test_redact_secrets_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    text = f"JWT_SECRET={jwt}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert jwt not in llm_text
    assert "eyJ" not in llm_text


def test_redact_secrets_high_entropy_catch_all():
    """A random-looking token with no known prefix, not in an assignment
    context — must still be caught by the entropy heuristic."""
    secret = "Xk9pQ2mR7vT4wZ8nB3jL6yH1cF5dS0aE"
    text = f"just paste this somewhere: {secret}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert secret not in llm_text
    assert any(r["kind"] == "high_entropy_token" for r in redactions)


def test_redact_secrets_low_entropy_token_not_flagged():
    """A long but LOW-entropy token (repetitive/dictionary-like) should not
    trip the high-entropy catch-all — avoids over-redacting ordinary long
    words/identifiers."""
    text = "the quick brown fox jumps over the lazy dog and then some more words"
    llm_text, redactions = redaction.redact_secrets(text)
    assert llm_text == text
    assert redactions == []


def test_redact_secrets_placeholders_are_stable_and_numbered():
    secret1 = "sk-ant-" + "a" * 30
    secret2 = "sk-ant-" + "b" * 30
    text = f"first: {secret1}\nsecond: {secret2}"
    llm_text, redactions = redaction.redact_secrets(text)
    assert "‹SECRET:anthropic_key#1›" in llm_text
    assert "‹SECRET:anthropic_key#2›" in llm_text
    assert len(redactions) == 2


def test_redact_secrets_never_includes_the_value_in_redactions():
    secret = "sk-ant-" + "z" * 30
    text = f"key: {secret}"
    _llm_text, redactions = redaction.redact_secrets(text)
    for r in redactions:
        assert set(r.keys()) == {"placeholder", "kind"}
        assert secret not in r["placeholder"]
        assert secret not in r["kind"]


def test_redact_secrets_overlapping_matches_resolved_by_priority():
    """A PEM block's internal base64 body would ALSO match the high-entropy
    catch-all in isolation — the whole block must be redacted as ONE
    pem_private_key span, not fragmented into multiple high-entropy hits."""
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP\n"
        "-----END PRIVATE KEY-----"
    )
    _llm_text, redactions = redaction.redact_secrets(pem)
    assert len(redactions) == 1
    assert redactions[0]["kind"] == "pem_private_key"


def test_redact_secrets_realistic_env_paste_no_false_negatives():
    """The load-bearing test: a realistic multi-secret .env-shaped paste —
    every distinct secret value must be absent from the redacted output."""
    secrets = {
        "db_password": "sup3rSecr3tPass",
        "openai_key": "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF",
        "aws_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret": "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY123456",
        "github_token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "anthropic_key": "sk-ant-api03-" + "q" * 40,
    }
    env_text = f"""
DATABASE_URL=postgres://admin:{secrets["db_password"]}@db.example.com:5432/mydb
OPENAI_API_KEY={secrets["openai_key"]}
AWS_ACCESS_KEY_ID={secrets["aws_key_id"]}
AWS_SECRET_ACCESS_KEY={secrets["aws_secret"]}
GITHUB_TOKEN={secrets["github_token"]}
ANTHROPIC_API_KEY={secrets["anthropic_key"]}
# a comment, not a secret
DEBUG=true
""".strip()

    llm_text, redactions = redaction.redact_secrets(env_text)

    for name, value in secrets.items():
        assert value not in llm_text, f"{name} leaked into redacted output"
    assert len(redactions) >= len(secrets)
    # Never leaks a value inside the redactions metadata either.
    for value in secrets.values():
        for r in redactions:
            assert value not in r["placeholder"]
    # Non-secret content survives untouched.
    assert "db.example.com" in llm_text
    assert "DEBUG=true" in llm_text
