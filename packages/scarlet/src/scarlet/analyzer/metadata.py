"""Feature metadata extraction via tree-sitter.

For one feature: parse all source files, extract structured data about
exports, components, hooks, slices, types. The output feeds the
generator (which builds CLAUDE.md skeletons + barrel exports) and the
LLM (which synthesizes prose from the structured data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser


@dataclass(frozen=True)
class ExportedSymbol:
    """A symbol exported from a feature file."""

    name: str
    kind: str  # component, hook, function, class, interface, type, slice, constant
    file_path: str
    is_default_export: bool
    line: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "is_default_export": self.is_default_export,
            "line": self.line,
        }


@dataclass(frozen=True)
class FeatureMetadata:
    """Structured metadata for one feature."""

    name: str
    path: str
    exports: list[ExportedSymbol] = field(default_factory=list)
    file_count: int = 0
    has_barrel: bool = False
    has_tests: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "exports": [e.to_dict() for e in self.exports],
            "file_count": self.file_count,
            "has_barrel": self.has_barrel,
            "has_tests": self.has_tests,
            "components": [e.to_dict() for e in self.exports if e.kind == "component"],
            "hooks": [e.to_dict() for e in self.exports if e.kind == "hook"],
            "slices": [e.to_dict() for e in self.exports if e.kind == "slice"],
            "types": [e.to_dict() for e in self.exports if e.kind in ("interface", "type")],
        }


# Languages that scarlet can parse
SUPPORTED_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs"}


def extract_feature_metadata(feature_path: Path) -> FeatureMetadata:
    """Walk a feature folder and extract structured metadata.

    Args:
        feature_path: Absolute path to the feature directory.

    Returns:
        FeatureMetadata with all exported symbols and counts.
    """
    feature_path = feature_path.resolve()

    exports: list[ExportedSymbol] = []
    file_count = 0
    has_tests = False

    for source_file in _discover_source_files(feature_path):
        file_count += 1
        if ".test." in source_file.name or ".spec." in source_file.name:
            has_tests = True
            continue

        try:
            file_exports = _extract_exports(source_file)
            exports.extend(file_exports)
        except Exception:
            # Tree-sitter parse failures shouldn't break the whole feature.
            continue

    has_barrel = _find_barrel(feature_path) is not None

    return FeatureMetadata(
        name=feature_path.name,
        path=str(feature_path),
        exports=exports,
        file_count=file_count,
        has_barrel=has_barrel,
        has_tests=has_tests,
    )


def _discover_source_files(feature_path: Path) -> list[Path]:
    """Walk a feature folder and yield indexable source files."""
    files: list[Path] = []
    for path in feature_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SUPPORTED_EXTENSIONS:
            continue
        # Skip generated, vendored, and dotfile-prefixed paths
        if any(part.startswith(".") for part in path.relative_to(feature_path).parts):
            continue
        files.append(path)
    return sorted(files)


def _find_barrel(feature_path: Path) -> Path | None:
    for name in ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs"):
        candidate = feature_path / name
        if candidate.exists():
            return candidate
    return None


def _get_parser(file_path: Path) -> Parser:
    """Build a tree-sitter parser for a given file's language."""
    if file_path.suffix in (".ts", ".tsx"):
        return Parser(language=Language(tstypescript.language_typescript()))
    return Parser(language=Language(tsjavascript.language()))


def _extract_exports(file_path: Path) -> list[ExportedSymbol]:
    """Parse a source file and extract its exported symbols.

    Recognizes:
      - export function Foo() {}            → function (or component if PascalCase)
      - export class Foo {}                 → class
      - export const Foo = ...              → constant or component (PascalCase)
      - export const useFoo = ...           → hook (use* prefix)
      - export interface Foo {}             → interface
      - export type Foo = ...               → type
      - export default function Foo() {}    → default export, classified by name
      - export default class Foo {}         → default export class
      - export { Foo, Bar }                 → re-exports (skipped — handled at barrel level)
    """
    source = file_path.read_text(encoding="utf-8")
    parser = _get_parser(file_path)
    tree = parser.parse(source.encode("utf-8"))

    exports: list[ExportedSymbol] = []
    for child in tree.root_node.children:
        if child.type == "export_statement":
            exports.extend(_extract_from_export_statement(child, str(file_path)))
    return exports


def _extract_from_export_statement(
    node: Node, file_path: str
) -> list[ExportedSymbol]:
    """Walk an export_statement and pull out the exported symbol(s)."""
    is_default = any(c.type == "default" for c in node.children)
    declaration = node.child_by_field_name("declaration")

    if declaration is None:
        # Handle `export default <identifier>;` — the name is exported by
        # reference to a function/const/class declared elsewhere in the file.
        # tree-sitter attaches the identifier as a direct child (not the
        # `declaration` field) in this form.
        if is_default:
            for child in node.children:
                if child.type == "identifier":
                    name = child.text.decode()
                    line = child.start_point[0] + 1
                    return [
                        _make_symbol(
                            name,
                            _classify_function(name),
                            file_path,
                            True,
                            line,
                        )
                    ]
        return []

    line = declaration.start_point[0] + 1

    match declaration.type:
        case "function_declaration":
            name = _name_of(declaration)
            return [_make_symbol(name, _classify_function(name), file_path, is_default, line)]

        case "class_declaration":
            name = _name_of(declaration)
            return [_make_symbol(name, "class", file_path, is_default, line)]

        case "interface_declaration":
            name = _name_of(declaration)
            return [_make_symbol(name, "interface", file_path, is_default, line)]

        case "type_alias_declaration":
            name = _name_of(declaration)
            return [_make_symbol(name, "type", file_path, is_default, line)]

        case "lexical_declaration" | "variable_declaration":
            name = _lexical_name(declaration)
            kind = _classify_function(name) if name else "constant"
            # `*Slice` and `*Api` are common Redux Toolkit conventions
            if name and (name.endswith("Slice") or name.endswith("slice")):
                kind = "slice"
            elif name and (name.endswith("Api") or name.endswith("api")):
                kind = "api"
            return [_make_symbol(name or "<anonymous>", kind, file_path, is_default, line)]

    return []


def _name_of(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node:
        return name_node.text.decode()
    return "<anonymous>"


def _lexical_name(node: Node) -> str | None:
    """Extract the variable name from a const/let declaration."""
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node:
                return name_node.text.decode()
    return None


def _classify_function(name: str) -> str:
    """Classify a function/constant by its name (React conventions).

    Precedence:
      1. `use*` hook pattern → hook
      2. SCREAMING_SNAKE_CASE → constant (e.g. `DASHBOARD_QUERY_STORAGE_KEY`,
         `QUOTES_FILTER_COLUMNS`, `CREATION_STEP`). A name is SCREAMING_SNAKE_CASE
         if stripping underscores/digits/$-signs leaves only uppercase letters.
      3. PascalCase (first char uppercase, not all-upper) → component
      4. Otherwise → function
    """
    if not name:
        return "function"
    if name.startswith("use") and len(name) > 3 and name[3].isupper():
        return "hook"
    alpha_only = "".join(c for c in name if c.isalpha())
    if alpha_only and alpha_only.isupper():
        return "constant"
    if name[0].isupper():
        return "component"
    return "function"


def _make_symbol(
    name: str, kind: str, file_path: str, is_default: bool, line: int
) -> ExportedSymbol:
    return ExportedSymbol(
        name=name,
        kind=kind,
        file_path=file_path,
        is_default_export=is_default,
        line=line,
    )
