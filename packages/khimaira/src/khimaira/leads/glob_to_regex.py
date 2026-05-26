"""Convert manifest glob paths to a Themis negative-lookahead allow-list regex.

The Themis block rules use a pattern of the form:
    ^(?!.*(?:allow1|allow2|...)).+

A path matching this regex is OUTSIDE the allow-list → Themis blocks the edit.
A path NOT matching (lookahead fires) is INSIDE the allow-list → edit allowed.

Glob rules
----------
- ``dir/**``       → ``dir/``         (prefix: any file under that directory)
- ``path/file.py`` → ``path/file\\.py`` (exact file, regex-escaped dots)
"""

from __future__ import annotations

import re


def glob_to_allow_fragment(glob_path: str) -> str:
    """Convert one glob pattern to a regex alternative for the allow-list.

    >>> glob_to_allow_fragment("packages/khimaira/src/khimaira/monitor/**")
    'packages/khimaira/src/khimaira/monitor/'
    >>> glob_to_allow_fragment("packages/khimaira/src/khimaira/monitor/sessions.py")
    'packages/khimaira/src/khimaira/monitor/sessions\\\\.py'
    """
    if glob_path.endswith("/**"):
        # Directory subtree: strip the glob suffix, keep trailing slash
        return glob_path[:-3] + "/"
    else:
        # Exact file path — escape regex metacharacters (primarily dots)
        return re.escape(glob_path)


def build_allow_regex(
    paths: list[str],
    role_doc_path: str,
    knowledge_doc_path: str,
) -> str:
    """Build the full negative-lookahead allow-list regex from manifest paths.

    The role doc and knowledge doc are always included in the allow-list so the
    lead can update its own files regardless of the path list in the manifest.

    Parameters
    ----------
    paths:
        Glob paths from the manifest (e.g. ``["packages/foo/**", "bar/baz.py"]``).
    role_doc_path:
        Absolute-or-relative path to the lead's own role doc (always allowed).
    knowledge_doc_path:
        Path to the lead's domain knowledge doc (always allowed).

    Returns
    -------
    str
        A regex string matching files OUTSIDE the allow-list (i.e. paths Themis
        should block). Use as the ``pattern`` field in the Themis YAML matcher.

    Examples
    --------
    >>> r = build_allow_regex(
    ...     ["packages/foo/**", "bar/baz.py"],
    ...     "roles/foo-lead.md",
    ...     "docs/domain/foo-knowledge.md",
    ... )
    >>> import re
    >>> bool(re.match(r, "packages/foo/something.py"))  # in-domain → no match
    False
    >>> bool(re.match(r, "packages/other/something.py"))  # out-of-domain → match → blocked
    True
    """
    fragments = [glob_to_allow_fragment(p) for p in paths]
    # Role doc and knowledge doc are always allowed
    fragments.append(re.escape(role_doc_path))
    fragments.append(re.escape(knowledge_doc_path))

    alternatives = "|".join(fragments)
    return f"^(?!.*(?:{alternatives})).+"
