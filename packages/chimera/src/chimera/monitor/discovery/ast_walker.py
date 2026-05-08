"""Tree-sitter AST fallback for topology extraction.

Used when runtime introspection (`introspector.py`) fails because the
target project's deps aren't importable in chimera's venv, or when a
graph factory builds node names dynamically (chimera's own factories).

Output is always marked `approximate: true` — the AST sees the literal
calls but cannot resolve string variables, list comprehensions, or
runtime-computed node names.

Patterns understood (verified against jeevy_portal's graphs):
  - StateGraph(...)                                                 ← marks a graph
  - add_node("name", ...)                                            ← node
  - add_edge("a", "b")                                               ← literal edge
  - add_edge(START, "x") / add_edge("x", END)                        ← identifier endpoints
  - add_edge(loop_var, END) inside `for loop_var in (...)`           ← unrolled
  - add_conditional_edges("source", _, {"label": "target", ...})     ← N edges
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .._optional import require
from .introspector import TopologyResult

_START_MARKER = "__start__"
_END_MARKER = "__end__"


@dataclass
class _ArgValue:
    """One positional argument resolved from the AST.

    `kind` is one of:
      - "string"       : a literal string. `text` = unquoted value.
      - "identifier"   : a bare identifier (e.g. START, END, lane). `text` = name.
      - "dict"         : a {str: str} dict literal. `mapping` is populated.
      - "other"        : anything we couldn't simplify.
    """

    kind: str
    text: str = ""
    mapping: dict[str, str] = field(default_factory=dict)


def _parser():
    if _PARSER_CACHE:
        return _PARSER_CACHE[0]
    tree_sitter = require("tree_sitter")
    tree_sitter_python = require("tree_sitter_python")
    language = tree_sitter.Language(tree_sitter_python.language())
    parser = tree_sitter.Parser(language)
    _PARSER_CACHE.append(parser)
    return parser


_PARSER_CACHE: list = []


def extract_from_path(path: Path) -> list[TopologyResult]:
    """Walk every .py file under `path` and extract StateGraph topologies."""
    if not path.is_dir():
        return [TopologyResult(error=f"not a directory: {path}", source="ast", approximate=True)]
    results: list[TopologyResult] = []
    for py_file in path.rglob("*.py"):
        if any(part in {".venv", "venv", "__pycache__", "node_modules", "site-packages"} for part in py_file.parts):
            continue
        results.extend(_extract_file(py_file))
    return results


def _extract_file(py_file: Path) -> list[TopologyResult]:
    try:
        source = py_file.read_bytes()
    except OSError:
        return []
    if b"StateGraph" not in source:
        return []

    parser = _parser()
    tree = parser.parse(source)
    root = tree.root_node

    function_scopes = list(_iter_function_definitions(root))
    function_scope_ids = {fn.id for fn in function_scopes}

    graphs: list[TopologyResult] = []

    # Module-level scope: every call NOT inside any function_definition
    module_calls = [c for c in _iter_calls(root) if not _is_inside_any(c, function_scope_ids)]
    module_loops = [
        f for f in _iter_for_statements(root) if not _is_inside_any(f, function_scope_ids)
    ]
    module_graph = _graph_from_scope(module_calls, module_loops, source, py_file.stem)
    if module_graph is not None:
        graphs.append(module_graph)

    for func_node in function_scopes:
        func_calls = list(_iter_calls(func_node))
        func_loops = list(_iter_for_statements(func_node))
        graph_name = _function_name(func_node, source) or py_file.stem
        graph = _graph_from_scope(func_calls, func_loops, source, graph_name)
        if graph is not None:
            graphs.append(graph)

    return graphs


def _graph_from_scope(
    calls,
    for_statements,
    source: bytes,
    name: str,
) -> TopologyResult | None:
    """Interpret a function (or module) body as a single StateGraph build."""
    loop_vars = _collect_loop_vars(for_statements, source)

    nodes: list[str] = []
    edges: list[tuple[str, str]] = []
    has_start = False
    has_end = False
    creates_graph = False

    for call in calls:
        method, args = _resolve_call(call, source)
        if method is None:
            continue

        if method == "StateGraph":
            creates_graph = True
            continue

        if method == "add_node" and args and args[0].kind == "string":
            nodes.append(args[0].text)
            continue

        if method == "add_edge" and len(args) >= 2:
            for src in _arg_values(args[0], loop_vars):
                for dst in _arg_values(args[1], loop_vars):
                    if src == _START_MARKER:
                        has_start = True
                    if dst == _END_MARKER:
                        has_end = True
                    edges.append((src, dst))
            continue

        if method == "add_conditional_edges" and args:
            source_node = _single_arg_value(args[0], loop_vars)
            if source_node is None:
                continue
            # Find the dict literal among the remaining positional args
            mapping: dict[str, str] = {}
            for arg in args[1:]:
                if arg.kind == "dict":
                    mapping = arg.mapping
                    break
            if not mapping:
                continue
            if source_node == _START_MARKER:
                has_start = True
            for target in mapping.values():
                if target == "END":
                    edges.append((source_node, _END_MARKER))
                    has_end = True
                else:
                    edges.append((source_node, target))
            continue

    if not creates_graph:
        return None

    if has_start and _START_MARKER not in nodes:
        nodes.insert(0, _START_MARKER)
    if has_end and _END_MARKER not in nodes:
        nodes.append(_END_MARKER)

    # Dedupe edges while preserving order — the same logical edge can be
    # emitted twice when a loop unrolls into multiple identical pairs.
    seen_edges: set[tuple[str, str]] = set()
    deduped_edges: list[tuple[str, str]] = []
    for edge in edges:
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        deduped_edges.append(edge)

    return TopologyResult(
        nodes=nodes,
        edges=deduped_edges,
        source="ast",
        approximate=True,
        graph_name=name,
    )


def _arg_values(arg: _ArgValue, loop_vars: dict[str, list[str]]) -> list[str]:
    """Resolve one positional arg to a list of possible string values.

    - strings → [literal]
    - START/END → [marker]
    - loop variable → all values in the loop's tuple
    - everything else → [] (drop the edge rather than emit junk)
    """
    if arg.kind == "string":
        return [arg.text]
    if arg.kind == "identifier":
        if arg.text == "START":
            return [_START_MARKER]
        if arg.text == "END":
            return [_END_MARKER]
        if arg.text in loop_vars and loop_vars[arg.text]:
            return list(loop_vars[arg.text])
    return []


def _single_arg_value(arg: _ArgValue, loop_vars: dict[str, list[str]]) -> str | None:
    values = _arg_values(arg, loop_vars)
    return values[0] if values else None


def _resolve_call(call_node, source: bytes) -> tuple[str | None, list[_ArgValue]]:
    """Return (method_name, [positional args]) for a tree-sitter `call` node.

    `args` preserves order. Each entry is an _ArgValue with kind set.
    """
    function = call_node.child_by_field_name("function")
    if function is None:
        return (None, [])

    if function.type == "attribute":
        attr = function.child_by_field_name("attribute")
        method = _text(attr, source) if attr else None
    elif function.type == "identifier":
        method = _text(function, source)
    else:
        method = None

    args_node = call_node.child_by_field_name("arguments")
    args: list[_ArgValue] = []
    if args_node is not None:
        for child in args_node.children:
            if child.type in ("(", ")", ","):
                continue
            args.append(_classify_arg(child, source))
    return (method, args)


def _classify_arg(node, source: bytes) -> _ArgValue:
    if node.type == "string":
        return _ArgValue(kind="string", text=_string_literal(node, source))
    if node.type == "identifier":
        return _ArgValue(kind="identifier", text=_text(node, source))
    if node.type == "dictionary":
        return _ArgValue(kind="dict", mapping=_dict_literal(node, source))
    return _ArgValue(kind="other", text=_text(node, source))


def _dict_literal(dict_node, source: bytes) -> dict[str, str]:
    """Extract a {string: string} dict literal. Skips non-string keys/values
    rather than coercing them to junk."""
    out: dict[str, str] = {}
    for child in dict_node.children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            continue
        if key_node.type != "string" or value_node.type != "string":
            continue
        out[_string_literal(key_node, source)] = _string_literal(value_node, source)
    return out


def _collect_loop_vars(for_statements, source: bytes) -> dict[str, list[str]]:
    """Map loop variables to the literal string values they iterate over.

    Only handles the `for x in (a, b, c)` and `for x in [a, b, c]` shapes
    where every element is a string literal — the common pattern jeevy
    uses (`for lane in ("chat_lane", "ingest_lane", ...)`).
    """
    out: dict[str, list[str]] = {}
    for fs in for_statements:
        var_node = fs.child_by_field_name("left")
        iter_node = fs.child_by_field_name("right")
        if var_node is None or iter_node is None:
            continue
        if var_node.type != "identifier":
            continue
        var_name = _text(var_node, source)
        values: list[str] = []
        if iter_node.type in ("tuple", "list"):
            for child in iter_node.children:
                if child.type == "string":
                    values.append(_string_literal(child, source))
        if values:
            out[var_name] = values
    return out


def _iter_function_definitions(node):
    """Yield every function_definition descendant (not the root)."""
    stack = [node]
    while stack:
        current = stack.pop()
        for child in current.children:
            if child.type == "function_definition":
                yield child
            stack.append(child)


def _iter_for_statements(node):
    """Yield every for_statement descendant within the given subtree."""
    stack = [node]
    while stack:
        current = stack.pop()
        for child in current.children:
            if child.type == "for_statement":
                yield child
            stack.append(child)


def _iter_calls(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "call":
            yield current
        for child in current.children:
            stack.append(child)


def _is_inside_any(node, function_ids: set) -> bool:
    """True if `node` has a function_definition ancestor in `function_ids`."""
    current = node.parent
    while current is not None:
        if current.id in function_ids:
            return True
        current = current.parent
    return False


def _function_name(func_node, source: bytes) -> str:
    if func_node.type != "function_definition":
        return ""
    name_node = func_node.child_by_field_name("name")
    return _text(name_node, source) if name_node else ""


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _string_literal(node, source: bytes) -> str:
    raw = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] in ("'", '"'):
        return raw[1:-1]
    return raw
