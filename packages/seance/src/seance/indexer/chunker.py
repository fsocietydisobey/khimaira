"""AST-based code chunking using tree-sitter.

Splits source files into semantic units (functions, classes, module-level code)
rather than arbitrary line ranges. Each chunk carries metadata about what it
represents and where it lives in the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
import tree_sitter_javascript as tsjavascript
from tree_sitter import Language, Parser, Node


class ChunkType(StrEnum):
    """The semantic type of a code chunk."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE = "module"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"
    EXPORT = "export"
    # Frontend-specific types
    COMPONENT = "component"  # React component
    HOOK = "hook"  # React hook
    COMPONENT_LOGIC = "component_logic"  # Sub-chunk: hooks + state within a component
    COMPONENT_HANDLERS = "component_handlers"  # Sub-chunk: event handlers within a component
    COMPONENT_RENDER = "component_render"  # Sub-chunk: JSX return within a component


# Components with bodies larger than this get sub-chunked
LARGE_COMPONENT_LINE_THRESHOLD = 80


def _is_pascal_case(name: str) -> bool:
    """Check if a name starts with an uppercase letter (React component convention)."""
    return bool(name) and name[0].isupper()


def _is_hook_name(name: str) -> bool:
    """Check if a name follows the React custom hook convention (useFoo)."""
    return bool(name) and name.startswith("use") and len(name) > 3 and name[3].isupper()


class SupportedLanguage(StrEnum):
    """Languages Séance can parse and chunk."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"


# File extension → language mapping
EXTENSION_MAP: dict[str, SupportedLanguage] = {
    ".py": SupportedLanguage.PYTHON,
    ".ts": SupportedLanguage.TYPESCRIPT,
    ".tsx": SupportedLanguage.TYPESCRIPT,
    ".js": SupportedLanguage.JAVASCRIPT,
    ".jsx": SupportedLanguage.JAVASCRIPT,
    ".mjs": SupportedLanguage.JAVASCRIPT,
}


@dataclass(frozen=True)
class CodeChunk:
    """A semantic unit of code extracted from a source file."""

    text: str
    file_path: str
    language: SupportedLanguage
    chunk_type: ChunkType
    symbol_name: str
    start_line: int
    end_line: int

    @property
    def chunk_id(self) -> str:
        """Deterministic ID for deduplication and updates."""
        return f"{self.file_path}::{self.symbol_name}::{self.start_line}"


def _get_language(lang: SupportedLanguage) -> Language:
    """Get the tree-sitter Language object for a supported language."""
    match lang:
        case SupportedLanguage.PYTHON:
            return Language(tspython.language())
        case SupportedLanguage.TYPESCRIPT:
            return Language(tstypescript.language_typescript())
        case SupportedLanguage.JAVASCRIPT:
            return Language(tsjavascript.language())


def detect_language(file_path: Path) -> SupportedLanguage | None:
    """Detect language from file extension. Returns None if unsupported."""
    return EXTENSION_MAP.get(file_path.suffix)


def chunk_file(file_path: Path, source: str | None = None) -> list[CodeChunk]:
    """Parse a source file and extract semantic code chunks.

    Args:
        file_path: Path to the source file.
        source: Optional pre-read source code. If None, reads from file_path.

    Returns:
        List of CodeChunk objects, one per semantic unit in the file.
    """
    lang = detect_language(file_path)
    if lang is None:
        return []

    if source is None:
        source = file_path.read_text(encoding="utf-8")

    parser = Parser(language=_get_language(lang))
    tree = parser.parse(source.encode("utf-8"))

    match lang:
        case SupportedLanguage.PYTHON:
            return _chunk_python(tree.root_node, source, str(file_path))
        case SupportedLanguage.TYPESCRIPT:
            return _chunk_typescript(tree.root_node, source, str(file_path))
        case SupportedLanguage.JAVASCRIPT:
            return _chunk_javascript(tree.root_node, source, str(file_path))


def _extract_text(source: str, node: Node) -> str:
    """Extract the source text for a tree-sitter node."""
    return source[node.start_byte:node.end_byte]


def _chunk_python(root: Node, source: str, file_path: str) -> list[CodeChunk]:
    """Extract chunks from a Python AST."""
    chunks: list[CodeChunk] = []

    for child in root.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            chunks.append(
                CodeChunk(
                    text=_extract_text(source, child),
                    file_path=file_path,
                    language=SupportedLanguage.PYTHON,
                    chunk_type=ChunkType.FUNCTION,
                    symbol_name=name_node.text.decode() if name_node else "<anonymous>",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
        elif child.type == "decorated_definition":
            # Unwrap decorator to get the actual definition
            inner = child.children[-1]
            if inner.type in ("function_definition", "class_definition"):
                name_node = inner.child_by_field_name("name")
                chunk_type = (
                    ChunkType.FUNCTION
                    if inner.type == "function_definition"
                    else ChunkType.CLASS
                )
                chunks.append(
                    CodeChunk(
                        text=_extract_text(source, child),
                        file_path=file_path,
                        language=SupportedLanguage.PYTHON,
                        chunk_type=chunk_type,
                        symbol_name=name_node.text.decode() if name_node else "<anonymous>",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
        elif child.type == "class_definition":
            name_node = child.child_by_field_name("name")
            class_name = name_node.text.decode() if name_node else "<anonymous>"

            # Class signature chunk (docstring + class-level statements, without method bodies)
            chunks.append(
                CodeChunk(
                    text=_extract_text(source, child),
                    file_path=file_path,
                    language=SupportedLanguage.PYTHON,
                    chunk_type=ChunkType.CLASS,
                    symbol_name=class_name,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )

            # Also extract individual methods
            body = child.child_by_field_name("body")
            if body:
                for stmt in body.children:
                    actual = stmt
                    if stmt.type == "decorated_definition":
                        actual = stmt.children[-1]

                    if actual.type == "function_definition":
                        method_name = actual.child_by_field_name("name")
                        chunks.append(
                            CodeChunk(
                                text=_extract_text(source, stmt),
                                file_path=file_path,
                                language=SupportedLanguage.PYTHON,
                                chunk_type=ChunkType.METHOD,
                                symbol_name=f"{class_name}.{method_name.text.decode() if method_name else '<anonymous>'}",
                                start_line=stmt.start_point[0] + 1,
                                end_line=stmt.end_point[0] + 1,
                            )
                        )

    # Module-level code that isn't a function or class definition
    module_lines: list[str] = []
    module_start: int | None = None
    module_end: int = 0

    for child in root.children:
        if child.type not in (
            "function_definition",
            "class_definition",
            "decorated_definition",
        ):
            text = _extract_text(source, child).strip()
            if text:
                if module_start is None:
                    module_start = child.start_point[0] + 1
                module_end = child.end_point[0] + 1
                module_lines.append(text)

    if module_lines and module_start is not None:
        chunks.append(
            CodeChunk(
                text="\n".join(module_lines),
                file_path=file_path,
                language=SupportedLanguage.PYTHON,
                chunk_type=ChunkType.MODULE,
                symbol_name="<module>",
                start_line=module_start,
                end_line=module_end,
            )
        )

    return chunks


def _chunk_typescript(root: Node, source: str, file_path: str) -> list[CodeChunk]:
    """Extract chunks from a TypeScript/TSX AST.

    Recognizes React components (PascalCase functions returning JSX) and
    custom hooks (use*) as distinct chunk types. Large components are
    sub-chunked into logic, handlers, and render sections.
    """
    chunks: list[CodeChunk] = []

    for child in root.children:
        match child.type:
            case "function_declaration":
                chunks.extend(
                    _make_function_chunks(child, source, file_path, SupportedLanguage.TYPESCRIPT)
                )
            case "class_declaration":
                name_node = child.child_by_field_name("name")
                chunks.append(
                    CodeChunk(
                        text=_extract_text(source, child),
                        file_path=file_path,
                        language=SupportedLanguage.TYPESCRIPT,
                        chunk_type=ChunkType.CLASS,
                        symbol_name=name_node.text.decode() if name_node else "<anonymous>",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            case "interface_declaration":
                name_node = child.child_by_field_name("name")
                chunks.append(
                    CodeChunk(
                        text=_extract_text(source, child),
                        file_path=file_path,
                        language=SupportedLanguage.TYPESCRIPT,
                        chunk_type=ChunkType.INTERFACE,
                        symbol_name=name_node.text.decode() if name_node else "<anonymous>",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            case "type_alias_declaration":
                name_node = child.child_by_field_name("name")
                chunks.append(
                    CodeChunk(
                        text=_extract_text(source, child),
                        file_path=file_path,
                        language=SupportedLanguage.TYPESCRIPT,
                        chunk_type=ChunkType.TYPE_ALIAS,
                        symbol_name=name_node.text.decode() if name_node else "<anonymous>",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            case "export_statement":
                # Unwrap export to get the actual declaration
                declaration = child.child_by_field_name("declaration")
                if declaration:
                    inner_chunks = _chunk_typescript_node(
                        declaration, source, file_path
                    )
                    chunks.extend(inner_chunks)
                else:
                    chunks.append(
                        CodeChunk(
                            text=_extract_text(source, child),
                            file_path=file_path,
                            language=SupportedLanguage.TYPESCRIPT,
                            chunk_type=ChunkType.EXPORT,
                            symbol_name="<export>",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                        )
                    )
            case "lexical_declaration":
                # const/let declarations — often arrow functions, components, or hooks
                chunks.extend(
                    _make_lexical_chunks(child, source, file_path, SupportedLanguage.TYPESCRIPT)
                )

    return chunks


def _classify_function_name(name: str) -> ChunkType:
    """Classify a function by its name using React conventions.

    PascalCase → component, use* → hook, otherwise → function.
    """
    if _is_hook_name(name):
        return ChunkType.HOOK
    if _is_pascal_case(name):
        return ChunkType.COMPONENT
    return ChunkType.FUNCTION


def _make_function_chunks(
    node: Node, source: str, file_path: str, language: SupportedLanguage
) -> list[CodeChunk]:
    """Create chunks from a function_declaration, classifying React components."""
    name_node = node.child_by_field_name("name")
    name = name_node.text.decode() if name_node else "<anonymous>"
    chunk_type = _classify_function_name(name)
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    full_chunk = CodeChunk(
        text=_extract_text(source, node),
        file_path=file_path,
        language=language,
        chunk_type=chunk_type,
        symbol_name=name,
        start_line=start_line,
        end_line=end_line,
    )

    chunks: list[CodeChunk] = [full_chunk]

    # If this is a large React component, also create sub-chunks
    if chunk_type == ChunkType.COMPONENT and (end_line - start_line) > LARGE_COMPONENT_LINE_THRESHOLD:
        body = node.child_by_field_name("body")
        if body:
            chunks.extend(_sub_chunk_component_body(body, source, file_path, name, language))

    return chunks


def _make_lexical_chunks(
    node: Node, source: str, file_path: str, language: SupportedLanguage
) -> list[CodeChunk]:
    """Create chunks from a lexical_declaration (const/let), classifying React components."""
    name = _extract_lexical_name(node)
    chunk_type = _classify_function_name(name)
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    full_chunk = CodeChunk(
        text=_extract_text(source, node),
        file_path=file_path,
        language=language,
        chunk_type=chunk_type,
        symbol_name=name,
        start_line=start_line,
        end_line=end_line,
    )

    chunks: list[CodeChunk] = [full_chunk]

    # If this is a large React component defined as an arrow function, sub-chunk its body
    if chunk_type == ChunkType.COMPONENT and (end_line - start_line) > LARGE_COMPONENT_LINE_THRESHOLD:
        arrow_body = _find_arrow_function_body(node)
        if arrow_body is not None:
            chunks.extend(_sub_chunk_component_body(arrow_body, source, file_path, name, language))

    return chunks


def _find_arrow_function_body(lexical_node: Node) -> Node | None:
    """Walk a lexical_declaration to find the body of an arrow function it contains."""
    for child in lexical_node.children:
        if child.type == "variable_declarator":
            value = child.child_by_field_name("value")
            if value and value.type == "arrow_function":
                return value.child_by_field_name("body")
    return None


def _sub_chunk_component_body(
    body: Node,
    source: str,
    file_path: str,
    component_name: str,
    language: SupportedLanguage,
) -> list[CodeChunk]:
    """Split a large component body into logical sub-chunks.

    Categorizes top-level statements into:
      - logic: useState, useEffect, useMemo, etc. + variable destructuring
      - handlers: event handler declarations (const handleX = ...)
      - render: the return statement with JSX
    """
    logic_statements: list[Node] = []
    handler_statements: list[Node] = []
    return_statement: Node | None = None

    for stmt in body.children:
        if stmt.type == "return_statement":
            return_statement = stmt
        elif stmt.type in ("lexical_declaration", "variable_declaration"):
            # Check if it looks like a handler (handle*, on*, starts with verb)
            name = _extract_lexical_name(stmt).lower()
            if name.startswith(("handle", "on")) or _is_handler_body(stmt):
                handler_statements.append(stmt)
            else:
                logic_statements.append(stmt)
        elif stmt.type == "expression_statement":
            # Hook calls at top of component (useEffect, etc.)
            logic_statements.append(stmt)

    sub_chunks: list[CodeChunk] = []

    if logic_statements:
        sub_chunks.append(
            _combine_statements(
                logic_statements,
                source,
                file_path,
                f"{component_name}.logic",
                ChunkType.COMPONENT_LOGIC,
                language,
            )
        )

    if handler_statements:
        sub_chunks.append(
            _combine_statements(
                handler_statements,
                source,
                file_path,
                f"{component_name}.handlers",
                ChunkType.COMPONENT_HANDLERS,
                language,
            )
        )

    if return_statement:
        sub_chunks.append(
            CodeChunk(
                text=_extract_text(source, return_statement),
                file_path=file_path,
                language=language,
                chunk_type=ChunkType.COMPONENT_RENDER,
                symbol_name=f"{component_name}.render",
                start_line=return_statement.start_point[0] + 1,
                end_line=return_statement.end_point[0] + 1,
            )
        )

    return sub_chunks


def _is_handler_body(node: Node) -> bool:
    """Heuristic: does this variable declaration contain an arrow function body?"""
    for child in node.children:
        if child.type == "variable_declarator":
            value = child.child_by_field_name("value")
            if value and value.type in ("arrow_function", "function_expression"):
                return True
    return False


def _combine_statements(
    statements: list[Node],
    source: str,
    file_path: str,
    symbol_name: str,
    chunk_type: ChunkType,
    language: SupportedLanguage,
) -> CodeChunk:
    """Combine multiple tree-sitter statements into a single chunk."""
    texts = [_extract_text(source, s) for s in statements]
    return CodeChunk(
        text="\n".join(texts),
        file_path=file_path,
        language=language,
        chunk_type=chunk_type,
        symbol_name=symbol_name,
        start_line=statements[0].start_point[0] + 1,
        end_line=statements[-1].end_point[0] + 1,
    )


def _chunk_typescript_node(
    node: Node, source: str, file_path: str
) -> list[CodeChunk]:
    """Extract chunks from a single TypeScript node (used for unwrapping exports)."""
    # Delegate to the same handlers used by the top-level walk, so export-wrapped
    # React components still get classified correctly and sub-chunked.
    if node.type == "function_declaration":
        return _make_function_chunks(node, source, file_path, SupportedLanguage.TYPESCRIPT)

    if node.type == "lexical_declaration":
        return _make_lexical_chunks(node, source, file_path, SupportedLanguage.TYPESCRIPT)

    name_node = node.child_by_field_name("name")
    name = name_node.text.decode() if name_node else "<anonymous>"

    chunk_type_map = {
        "class_declaration": ChunkType.CLASS,
        "interface_declaration": ChunkType.INTERFACE,
        "type_alias_declaration": ChunkType.TYPE_ALIAS,
    }

    chunk_type = chunk_type_map.get(node.type, ChunkType.EXPORT)

    return [
        CodeChunk(
            text=_extract_text(source, node),
            file_path=file_path,
            language=SupportedLanguage.TYPESCRIPT,
            chunk_type=chunk_type,
            symbol_name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
    ]


def _extract_lexical_name(node: Node) -> str:
    """Extract the variable name from a const/let declaration."""
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node:
                return name_node.text.decode()
    return "<anonymous>"


def _chunk_javascript(root: Node, source: str, file_path: str) -> list[CodeChunk]:
    """Extract chunks from a JavaScript AST. Reuses TypeScript logic since the AST shapes are similar."""
    chunks = _chunk_typescript(root, source, file_path)
    # Fix language on all chunks
    return [
        CodeChunk(
            text=c.text,
            file_path=c.file_path,
            language=SupportedLanguage.JAVASCRIPT,
            chunk_type=c.chunk_type,
            symbol_name=c.symbol_name,
            start_line=c.start_line,
            end_line=c.end_line,
        )
        for c in chunks
    ]
