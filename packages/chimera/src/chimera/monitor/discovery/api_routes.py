"""FastAPI route extractor — maps HTTP routes to handler functions and
detects which routes invoke a LangGraph graph.

Used by the full-stack-trace skill to follow user actions through the
stack: UI click → API call → graph invocation → DB write. Without this
extractor, that trace requires manual grep-the-codebase work.

Static AST analysis:
  - No imports of the target project (works for any FastAPI app
    regardless of whether its deps are in chimera's venv)
  - Returns "approximate" semantics: dynamic registrations
    (e.g. `app.add_api_route(path, fn)` with a runtime path) won't
    be captured perfectly, but decorator-based routes will
  - Detects graph invocations inside handler bodies by pattern
    matching (`.ainvoke`, `.astream`, calls to known graph builders)

Patterns understood:
  - @app.<method>("/path")              decorator on the FastAPI app
  - @router.<method>("/path")           decorator on an APIRouter
  - app.include_router(router, prefix=) sub-router registration
  - APIRouter(prefix="/foo")            router-level prefix

Limitations (knowingly):
  - No support for non-Python frameworks (Express, etc.) — add adapters
    if those ever land in chimera's scope
  - Programmatic add_api_route / add_route calls aren't captured
  - Path variables (`/items/{id}`) are returned literally
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from chimera.log import get_logger

log = get_logger("monitor.discovery.api_routes")

# HTTP-method decorator names FastAPI exposes on app + router objects.
_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options"}
)

# Heuristic patterns for "this handler invokes a graph somehow."
# The file is decoded into AST; we walk the handler body and any
# function it directly calls, looking for these clues.
_GRAPH_INVOKE_ATTRS = frozenset({"ainvoke", "astream", "invoke", "stream"})

# Skip directories typical of dependency installs / build outputs.
_SKIP_DIRS = frozenset({
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    "site-packages", ".next", "dist", "build", ".tox",
})


@dataclass
class APIRoute:
    """One HTTP route with metadata + graph-invocation indicators."""

    method: str                         # GET / POST / PUT / ...
    path: str                           # full path (router prefix + decorator path)
    handler: str                        # function name
    file: str                           # relative path to the source file
    line: int                           # line number of the @decorator
    invokes_graph: bool                 # does the handler appear to invoke a graph?
    graph_hints: list[str] = field(default_factory=list)  # what we matched
    request_model: str | None = None    # name of the request body type (if annotated)
    response_model: str | None = None   # name of the response model (if declared)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "handler": self.handler,
            "file": self.file,
            "line": self.line,
            "invokes_graph": self.invokes_graph,
            "graph_hints": self.graph_hints,
            "request_model": self.request_model,
            "response_model": self.response_model,
        }


def extract_from_path(project_path: Path) -> list[APIRoute]:
    """Walk a project's Python files, extract FastAPI routes."""
    if not project_path.is_dir():
        log.warning("api_routes: not a directory: %s", project_path)
        return []

    all_routes: list[APIRoute] = []
    for py_file in _iter_python_files(project_path):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "fastapi" not in text.lower() and "APIRouter" not in text:
            # Cheap pre-filter — file doesn't import or reference FastAPI
            continue
        try:
            tree = ast.parse(text, filename=str(py_file))
        except SyntaxError:
            log.debug("api_routes: skip un-parseable %s", py_file)
            continue
        rel = py_file.relative_to(project_path).as_posix()
        all_routes.extend(_extract_from_module(tree, rel))

    return all_routes


def _iter_python_files(project_path: Path):
    for p in project_path.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _extract_from_module(tree: ast.Module, rel_file: str) -> list[APIRoute]:
    """Find APIRouter prefixes + walk every decorated handler in the module."""
    # First pass — find APIRouter(...) instantiations and capture prefixes.
    # Used to combine with each `@router.get("/path")` decorator below.
    router_prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        if not _is_call_to_name(node.value.func, "APIRouter"):
            continue
        for kw in node.value.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                # node.targets is the LHS — typically a single Name
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        router_prefixes[target.id] = str(kw.value.value)

    # Second pass — find @<router>.<method>("/path") decorators on functions
    routes: list[APIRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            route = _route_from_decorator(dec, node, rel_file, router_prefixes)
            if route is not None:
                routes.append(route)
    return routes


def _route_from_decorator(
    dec: ast.expr,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_file: str,
    router_prefixes: dict[str, str],
) -> APIRoute | None:
    """Match `@<obj>.<method>("/path", ...)` and build an APIRoute."""
    if not isinstance(dec, ast.Call):
        return None
    if not isinstance(dec.func, ast.Attribute):
        return None
    method = dec.func.attr.lower()
    if method not in _HTTP_METHODS:
        return None

    # The object the decorator is attached to (app, router, sub_router, …)
    obj = dec.func.value
    obj_name = obj.id if isinstance(obj, ast.Name) else None
    prefix = router_prefixes.get(obj_name or "", "")

    # First positional arg is the path
    path = ""
    if dec.args and isinstance(dec.args[0], ast.Constant):
        path = str(dec.args[0].value)

    full_path = f"{prefix}{path}" if path.startswith("/") else f"{prefix}/{path}"
    full_path = full_path.rstrip("/") or "/"

    # Response model from `response_model=...` keyword
    response_model: str | None = None
    for kw in dec.keywords:
        if kw.arg == "response_model":
            response_model = _ann_repr(kw.value)
            break

    # Request body — first parameter with a non-Depends annotation that looks
    # like a Pydantic model. Cheap heuristic.
    request_model = _detect_request_model(fn)

    # Graph-invocation hints from the function body
    invokes_graph, hints = _detect_graph_invocation(fn)

    return APIRoute(
        method=method.upper(),
        path=full_path,
        handler=fn.name,
        file=rel_file,
        line=dec.lineno,
        invokes_graph=invokes_graph,
        graph_hints=hints,
        request_model=request_model,
        response_model=response_model,
    )


def _detect_request_model(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """First positional parameter whose annotation isn't Depends/path/query."""
    skip_names = {"Depends", "Query", "Path", "Header", "Cookie", "Body", "Form", "File"}
    for arg in fn.args.args:
        if arg.arg in {"self", "request"}:
            continue
        if arg.annotation is None:
            continue
        ann = arg.annotation
        # Strip Annotated[...] wrappers
        if isinstance(ann, ast.Subscript) and _ann_repr(ann.value) == "Annotated":
            ann = ann.slice
            if isinstance(ann, ast.Tuple) and ann.elts:
                ann = ann.elts[0]
        # Skip if annotation reduces to something in skip_names
        repr_ = _ann_repr(ann)
        if any(s in repr_ for s in skip_names):
            continue
        return repr_
    return None


def _detect_graph_invocation(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, list[str]]:
    """Walk the function body looking for graph-invocation patterns.

    Cheap heuristic — false positives possible (e.g., `session.invoke()`
    is unrelated). False negatives also possible if the handler dispatches
    work to a helper that calls .ainvoke. Good enough for the trace skill
    to surface candidates; user verifies by reading the handler.
    """
    hints: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        # Pattern: <something>.ainvoke(...) or .astream(...)
        if isinstance(node.func, ast.Attribute) and node.func.attr in _GRAPH_INVOKE_ATTRS:
            attr = node.func.attr
            target = _ann_repr(node.func.value)
            hints.append(f"{target}.{attr}()")
        # Pattern: build_<something>_graph(...)
        elif isinstance(node.func, ast.Name) and node.func.id.startswith("build_") and "graph" in node.func.id:
            hints.append(f"{node.func.id}()")
        # Pattern: chimera tool fan-out (chain_pipeline, swarm, etc.)
        elif isinstance(node.func, ast.Name) and node.func.id in {
            "chain_pipeline", "chain_refiner", "chain_hypervisor", "swarm",
            "chain_components", "chain_deadcode", "chain_toolbuilder",
        }:
            hints.append(f"{node.func.id}()")
    return (bool(hints), hints)


def _is_call_to_name(node: ast.expr, name: str) -> bool:
    """Whether `node` is a Name(name) or an Attribute ending in name."""
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, ast.Attribute):
        return node.attr == name
    return False


def _ann_repr(node: ast.expr) -> str:
    """Best-effort string repr of an annotation expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ann_repr(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_ann_repr(node.value)}[…]"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>"
