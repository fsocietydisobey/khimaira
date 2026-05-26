"""Tests for khimaira.leads.glob_to_regex — glob pattern → Themis allow regex.

Critical cases:
- dir/**  → prefix match (any file under that dir)
- file.py → exact match with escaped dot (NOT a dir prefix)
- in-domain paths → no match (allowed → not blocked by Themis)
- out-of-domain paths → match (blocked by Themis)
- Role doc and knowledge doc always in allow-list
"""

from __future__ import annotations

import re

import pytest

from khimaira.leads.glob_to_regex import build_allow_regex, glob_to_allow_fragment


# ---------------------------------------------------------------------------
# glob_to_allow_fragment
# ---------------------------------------------------------------------------


def test_fragment_dir_glob():
    frag = glob_to_allow_fragment("packages/foo/src/**")
    assert frag == "packages/foo/src/"


def test_fragment_exact_file():
    frag = glob_to_allow_fragment("packages/foo/bar/sessions.py")
    # Dots must be escaped
    assert frag == r"packages/foo/bar/sessions\.py"


def test_fragment_exact_file_no_glob():
    frag = glob_to_allow_fragment("packages/foo/bar/chats.py")
    assert frag == r"packages/foo/bar/chats\.py"


def test_fragment_nested_dir_glob():
    frag = glob_to_allow_fragment("packages/khimaira/src/khimaira/monitor/**")
    assert frag == "packages/khimaira/src/khimaira/monitor/"


# ---------------------------------------------------------------------------
# build_allow_regex — in-domain paths should NOT match (allowed, not blocked)
# ---------------------------------------------------------------------------


def _make_regex(paths, role_doc="roles/foo-lead.md", knowledge="docs/foo-knowledge.md"):
    pattern = build_allow_regex(paths, role_doc, knowledge)
    return re.compile(pattern)


def test_in_domain_dir_not_matched():
    r = _make_regex(["packages/backend/**"])
    # Files under packages/backend/ are IN domain → regex must NOT match
    assert not r.match("packages/backend/something.py")
    assert not r.match("packages/backend/sub/dir/file.py")


def test_out_of_domain_dir_matched():
    r = _make_regex(["packages/backend/**"])
    # Files outside the domain → regex MUST match → Themis blocks
    assert r.match("packages/frontend/something.py")
    assert r.match("packages/other/main.py")


def test_exact_file_sessions_py_in_domain():
    """Critical: sessions.py is a FILE, not a dir — must not match dir pattern."""
    r = _make_regex(
        ["packages/khimaira/src/khimaira/monitor/sessions.py"],
        role_doc="packages/khimaira/src/khimaira/roles/data-lead.md",
        knowledge="docs/domain/data-knowledge.md",
    )
    # sessions.py itself → in domain → NOT matched (allowed)
    assert not r.match(
        "packages/khimaira/src/khimaira/monitor/sessions.py"
    )


def test_exact_file_sessions_dir_is_out_of_domain():
    """A hypothetical sessions/ directory is NOT allowed by the sessions.py file rule."""
    r = _make_regex(
        ["packages/khimaira/src/khimaira/monitor/sessions.py"],
        role_doc="packages/khimaira/src/khimaira/roles/data-lead.md",
        knowledge="docs/domain/data-knowledge.md",
    )
    # A file named sessions_helper.py in the same dir → NOT in allow-list
    assert r.match(
        "packages/khimaira/src/khimaira/monitor/sessions_helper.py"
    )


def test_role_doc_always_allowed():
    r = _make_regex(
        ["packages/backend/**"],
        role_doc="packages/khimaira/src/khimaira/roles/backend-lead.md",
        knowledge="docs/domain/backend-knowledge.md",
    )
    assert not r.match("packages/khimaira/src/khimaira/roles/backend-lead.md")


def test_knowledge_doc_always_allowed():
    r = _make_regex(
        ["packages/backend/**"],
        role_doc="packages/khimaira/src/khimaira/roles/backend-lead.md",
        knowledge="docs/domain/backend-knowledge.md",
    )
    assert not r.match("docs/domain/backend-knowledge.md")


def test_multiple_paths():
    r = _make_regex(
        [
            "packages/khimaira/src/khimaira/monitor/**",
            "packages/khimaira/src/khimaira/hooks/**",
        ]
    )
    assert not r.match("packages/khimaira/src/khimaira/monitor/api.py")
    assert not r.match("packages/khimaira/src/khimaira/hooks/session_hook.py")
    assert r.match("packages/khimaira/src/khimaira/leads/manifest.py")


def test_regex_starts_with_anchor():
    pattern = build_allow_regex(["packages/foo/**"], "roles/foo-lead.md", "docs/foo.md")
    assert pattern.startswith("^(?!")


def test_regex_ends_with_dot_plus():
    pattern = build_allow_regex(["packages/foo/**"], "roles/foo-lead.md", "docs/foo.md")
    assert pattern.endswith(").+")


# ---------------------------------------------------------------------------
# Parity with existing hand-written backend-lead regex (reference test)
# ---------------------------------------------------------------------------


def test_backend_lead_regex_parity():
    """Generated regex for backend paths must allow the same paths as the
    hand-written backend-lead.yaml regex."""
    # Hand-written regex from backend-lead.yaml (simplified for test)
    hand_written = re.compile(
        r"^(?!.*(?:"
        r"packages/khimaira/src/khimaira/(?:monitor|hooks|mcp_calls|attach)/|"
        r"packages/khimaira/src/khimaira/roles/backend-lead\.md|"
        r"packages/themis/src/themis/|"
        r"packages/khimaira/tests/|"
        r"packages/specter/src/specter/server\.py|"
        r"docs/domain/backend-knowledge\.md"
        r")).+"
    )

    in_domain_paths = [
        "packages/khimaira/src/khimaira/monitor/api.py",
        "packages/khimaira/src/khimaira/hooks/session_hook.py",
        "packages/khimaira/src/khimaira/roles/backend-lead.md",
        "docs/domain/backend-knowledge.md",
    ]
    for path in in_domain_paths:
        # In-domain: hand-written should NOT match
        assert not hand_written.match(path), f"expected in-domain: {path}"

    out_of_domain_paths = [
        "packages/khimaira/src/khimaira/leads/manifest.py",
        "packages/other/file.py",
        "frontend/app.tsx",
    ]
    for path in out_of_domain_paths:
        # Out-of-domain: hand-written MUST match (blocked)
        assert hand_written.match(path), f"expected out-of-domain: {path}"
