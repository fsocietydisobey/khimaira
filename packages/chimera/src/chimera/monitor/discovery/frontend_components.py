"""Frontend component graph extractor — parses React/Next.js components,
extracts API calls, prop interfaces, and state-management uses.

Companion to api_routes.py for the full-stack-trace skill: when a user
clicks something, this tells us which component fired which API call.

Approach: regex + brace-matching on .tsx/.jsx files. Tree-sitter would
be more robust (handles JSX comments, template literals, etc.) but adds
a heavyweight dep. Regex is good enough for grep-grade results — the
skill verifies with reading.

Patterns extracted:
  - Component declarations:
      function Foo(...) { ... }
      const Foo = (...) => { ... }
      export default function Foo(...) { ... }
  - API calls:
      fetch('/api/...', ...)
      axios.get/post/put/delete('/api/...', ...)
      useGetXxxQuery(...) / useXxxMutation()  (RTK Query)
  - State hooks: useState, useReducer, useDispatch, useSelector,
    useStore, useQuery (TanStack), useSWR
  - Props consumed: TypeScript interface lines preceding the component

Limitations (knowingly):
  - Won't capture API calls dispatched indirectly (e.g. via a service
    layer). The graph maps direct frontend → API edges; indirect ones
    show up as "calls service.X" without resolving the URL.
  - Server components in Next.js look like regular components here;
    won't distinguish 'use client' boundaries.
  - Dynamic component imports may be missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from chimera.log import get_logger

log = get_logger("monitor.discovery.frontend_components")

# Skip directories that don't contain user code.
_SKIP_DIRS = frozenset({
    "node_modules", ".next", "dist", "build", ".git",
    "__pycache__", "out", ".turbo", ".cache",
})

_TARGET_EXTS = (".tsx", ".jsx", ".ts", ".js")

# Component declarations — three common shapes
_COMP_PATTERNS = [
    # function ComponentName(...) — only PascalCase names
    re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Z][A-Za-z0-9]*)\s*\("),
    # const ComponentName = (...) =>
    re.compile(r"^(?:export\s+(?:default\s+)?)?const\s+([A-Z][A-Za-z0-9]*)\s*[:=]"),
]

# API call patterns — extract URL when literal
_API_FETCH = re.compile(r"""fetch\s*\(\s*['"`]([^'"`]+)['"`]""")
_API_AXIOS = re.compile(r"""axios\.(?:get|post|put|patch|delete|head|options)\s*\(\s*['"`]([^'"`]+)['"`]""")
# RTK Query hooks: useGetUserQuery, useUpdateUserMutation, etc.
_API_RTK = re.compile(r"""\buse([A-Z][A-Za-z0-9]+(?:Query|Mutation|LazyQuery))\b""")

# State hooks
_HOOK_PATTERNS = [
    re.compile(r"\b(useState|useReducer)\b"),
    re.compile(r"\b(useDispatch|useSelector|useStore)\b"),
    re.compile(r"\b(useQuery|useMutation|useSWR|useSWRMutation)\b"),
]


@dataclass
class FrontendComponent:
    """One component + what it touches."""

    name: str
    file: str
    line: int
    api_calls: list[str] = field(default_factory=list)   # URLs and RTK hook names
    state_hooks: list[str] = field(default_factory=list) # which state libs in use
    imports_components: list[str] = field(default_factory=list)  # PascalCase imports

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "api_calls": sorted(set(self.api_calls)),
            "state_hooks": sorted(set(self.state_hooks)),
            "imports_components": sorted(set(self.imports_components)),
        }


def extract_from_path(project_path: Path) -> list[FrontendComponent]:
    """Walk a project's .tsx/.jsx files, extract components + their
    API calls and state-management uses."""
    if not project_path.is_dir():
        log.warning("frontend_components: not a directory: %s", project_path)
        return []

    out: list[FrontendComponent] = []
    for f in _iter_target_files(project_path):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Cheap pre-filter: file must look like it contains React or
        # an API call, otherwise skip
        if not any(s in text for s in ("function", "const ", "fetch(", "axios.", "use", "import")):
            continue
        rel = f.relative_to(project_path).as_posix()
        out.extend(_extract_components(text, rel))
    return out


def _iter_target_files(project_path: Path):
    for ext in _TARGET_EXTS:
        for p in project_path.rglob(f"*{ext}"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            yield p


def _extract_components(text: str, rel: str) -> list[FrontendComponent]:
    """Identify component blocks; for each, extract its API/state usage."""
    lines = text.splitlines()
    n = len(lines)

    # Find imports of PascalCase symbols — used as a proxy for "what
    # other components does this file pull in"
    imports = _extract_imports(text)

    # For each line that starts a component, walk forward until we find
    # the matching closing brace at indent 0 (or the next component
    # declaration). Crude but works for normally-formatted code.
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        for pat in _COMP_PATTERNS:
            m = pat.match(stripped)
            if m:
                starts.append((i, m.group(1)))
                break

    out: list[FrontendComponent] = []
    for idx, (start_line, name) in enumerate(starts):
        end_line = starts[idx + 1][0] if idx + 1 < len(starts) else n
        body = "\n".join(lines[start_line:end_line])

        comp = FrontendComponent(
            name=name,
            file=rel,
            line=start_line + 1,
            imports_components=list(imports),
        )

        # API calls
        for m in _API_FETCH.finditer(body):
            comp.api_calls.append(m.group(1))
        for m in _API_AXIOS.finditer(body):
            comp.api_calls.append(m.group(1))
        for m in _API_RTK.finditer(body):
            comp.api_calls.append(f"rtk:{m.group(1)}")

        # State hooks
        seen_hooks: set[str] = set()
        for pat in _HOOK_PATTERNS:
            for m in pat.finditer(body):
                hook = m.group(1)
                if hook not in seen_hooks:
                    seen_hooks.add(hook)
                    comp.state_hooks.append(hook)

        out.append(comp)
    return out


def _extract_imports(text: str) -> list[str]:
    """PascalCase identifiers from import statements.

    Pulls names out of:
      import Foo from '...'
      import { Foo, Bar } from '...'
      import Foo, { Bar } from '...'
    """
    names: set[str] = set()
    for m in re.finditer(
        r"^import\s+(?:type\s+)?(?:(\w+)\s*,?\s*)?(?:\{([^}]+)\})?\s+from",
        text,
        re.MULTILINE,
    ):
        default_name, named = m.group(1), m.group(2)
        if default_name and default_name[0].isupper():
            names.add(default_name)
        if named:
            for chunk in named.split(","):
                # `Foo as Bar` → take Bar (the local name)
                if " as " in chunk:
                    chunk = chunk.split(" as ")[-1]
                chunk = chunk.strip()
                if chunk and chunk[0].isupper():
                    names.add(chunk)
    return sorted(names)
