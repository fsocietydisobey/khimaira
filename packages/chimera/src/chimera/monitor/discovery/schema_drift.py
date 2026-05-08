"""Schema drift detector — compare Pydantic models in a project's
source against the actual Postgres schema.

Why this matters: Pydantic models declare the shape backend code expects;
the DB has the shape that actually exists. Drift = bug that surfaces as
runtime ValidationError or worse, silent data loss.

Approach:
  - Walk .py files for `class X(BaseModel)` declarations
  - Extract field names + annotated types
  - For each model with a name suggesting a table mapping (configurable
    convention: snake_case the class name, or trust __tablename__ /
    __table_name__ markers in the class body)
  - Query Postgres for the table's column names + types
  - Diff: extra fields in Pydantic / extra columns in DB / type mismatches

Caveats:
  - Type comparison is fuzzy (str vs VARCHAR, datetime vs TIMESTAMP).
    We normalize to coarse buckets ('text', 'int', 'float', 'bool',
    'datetime', 'json', 'binary', 'unknown'). Refinement only when both
    sides agree on the bucket.
  - Pydantic models that don't map to a table are silently ignored.
    Surface 'Pydantic model with no matching table' as informational
    rather than an error — many models are request/response DTOs.
  - SQLAlchemy ORM models map differently — if the project uses ORM,
    use the ORM's __tablename__ instead of the convention. (Detected
    via the same regex; if found, takes precedence over conventions.)
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chimera.log import get_logger

log = get_logger("monitor.discovery.schema_drift")

_SKIP_DIRS = frozenset({
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    "site-packages", ".next", "dist", "build", ".tox",
})


@dataclass
class PydanticModel:
    """One Pydantic class extracted from source."""

    name: str                  # class name
    file: str                  # relative source path
    line: int
    table_name: str            # derived from convention or __tablename__
    fields: dict[str, str] = field(default_factory=dict)  # name → annotation


@dataclass
class DriftReport:
    """One model-vs-table comparison."""

    model: str                 # Pydantic class name
    file: str
    line: int
    table: str                 # DB table name
    table_exists: bool         # was the table found in the DB?
    only_in_model: list[str] = field(default_factory=list)
    only_in_db: list[str] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return (
            not self.table_exists
            or bool(self.only_in_model)
            or bool(self.only_in_db)
            or bool(self.type_mismatches)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "file": self.file,
            "line": self.line,
            "table": self.table,
            "table_exists": self.table_exists,
            "only_in_model": self.only_in_model,
            "only_in_db": self.only_in_db,
            "type_mismatches": self.type_mismatches,
            "has_drift": self.has_drift,
        }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def extract_models(project_path: Path) -> list[PydanticModel]:
    """Walk .py files, find `class X(BaseModel)` declarations."""
    if not project_path.is_dir():
        return []

    models: list[PydanticModel] = []
    for py_file in _iter_python_files(project_path):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "BaseModel" not in text and "DeclarativeBase" not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(py_file))
        except SyntaxError:
            continue
        rel = py_file.relative_to(project_path).as_posix()
        models.extend(_extract_from_module(tree, rel))
    return models


def diff_against_postgres(
    models: list[PydanticModel],
    postgres_url: str,
) -> list[DriftReport]:
    """Compare each model's fields to the corresponding DB table."""
    import psycopg

    table_schemas = _fetch_table_schemas(postgres_url)
    reports: list[DriftReport] = []
    for m in models:
        if not m.fields:
            continue
        table = table_schemas.get(m.table_name)
        if table is None:
            reports.append(DriftReport(
                model=m.name,
                file=m.file,
                line=m.line,
                table=m.table_name,
                table_exists=False,
            ))
            continue

        model_fields = {f: _normalize_type(t) for f, t in m.fields.items()}
        only_in_model = sorted(set(model_fields) - set(table))
        only_in_db = sorted(set(table) - set(model_fields))
        mismatches: list[dict[str, str]] = []
        for fname in set(model_fields) & set(table):
            model_t = model_fields[fname]
            db_t = _normalize_type(table[fname])
            if model_t != "unknown" and db_t != "unknown" and model_t != db_t:
                mismatches.append({
                    "field": fname,
                    "model_type": model_t,
                    "db_type": db_t,
                })
        reports.append(DriftReport(
            model=m.name,
            file=m.file,
            line=m.line,
            table=m.table_name,
            table_exists=True,
            only_in_model=only_in_model,
            only_in_db=only_in_db,
            type_mismatches=mismatches,
        ))
    return reports


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _iter_python_files(project_path: Path):
    for p in project_path.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _extract_from_module(tree: ast.Module, rel_file: str) -> list[PydanticModel]:
    """Find every `class X(BaseModel)` (or SQLAlchemy DeclarativeBase)
    in the module and pull its annotated fields out."""
    models: list[PydanticModel] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not _looks_like_model(node):
            continue
        fields = _extract_fields(node)
        if not fields:
            continue
        table_name = _detect_table_name(node) or _to_snake_case(node.name)
        models.append(PydanticModel(
            name=node.name,
            file=rel_file,
            line=node.lineno,
            table_name=table_name,
            fields=fields,
        ))
    return models


def _looks_like_model(cls: ast.ClassDef) -> bool:
    """Crude check: inherits from BaseModel or DeclarativeBase or
    has __tablename__."""
    for base in cls.bases:
        name = _ann_repr(base)
        if "BaseModel" in name or "DeclarativeBase" in name or "Base" == name:
            return True
    # Also count any class with a __tablename__ literal as a model
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in ("__tablename__", "__table_name__"):
                    return True
    return False


def _extract_fields(cls: ast.ClassDef) -> dict[str, str]:
    """Return name → annotation-as-string for annotated attributes."""
    out: dict[str, str] = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out[stmt.target.id] = _ann_repr(stmt.annotation)
    return out


def _detect_table_name(cls: ast.ClassDef) -> str | None:
    """SQLAlchemy / explicit `__tablename__ = "foo"`."""
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for tgt in stmt.targets:
            if isinstance(tgt, ast.Name) and tgt.id in ("__tablename__", "__table_name__"):
                if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    return stmt.value.value
    return None


def _to_snake_case(name: str) -> str:
    """`UserProfile` → `user_profile`."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _ann_repr(node: ast.expr) -> str:
    """Best-effort string of an annotation."""
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


# Coarse type buckets — comparison only fires when both sides agree.
_TYPE_BUCKETS = {
    "str": "text", "string": "text", "varchar": "text", "text": "text",
    "char": "text", "uuid": "text",  # UUID-as-text is common
    "int": "int", "integer": "int", "bigint": "int", "smallint": "int",
    "float": "float", "real": "float", "double": "float",
    "numeric": "float", "decimal": "float",
    "bool": "bool", "boolean": "bool",
    "datetime": "datetime", "timestamp": "datetime", "date": "datetime",
    "time": "datetime", "timestamptz": "datetime",
    "dict": "json", "list": "json", "json": "json", "jsonb": "json",
    "bytes": "binary", "bytea": "binary", "blob": "binary",
}


def _normalize_type(t: str) -> str:
    """Reduce a type annotation / DB type to a coarse bucket. Returns
    'unknown' when we can't classify — comparisons on 'unknown' are
    skipped to avoid false-positive mismatches."""
    if not t:
        return "unknown"
    s = t.lower().strip()
    # Strip Optional[...] / Annotated[...] / List[...] wrappers
    s = re.sub(r"^(optional|annotated|list|tuple|dict|union)\[", "", s)
    s = s.split("[")[0]  # take the head of the type expression
    # Trim any trailing junk
    s = s.rstrip("]…").strip()
    return _TYPE_BUCKETS.get(s, "unknown")


def _fetch_table_schemas(postgres_url: str) -> dict[str, dict[str, str]]:
    """Returns {table_name: {column_name: data_type}} for the public schema."""
    import psycopg

    out: dict[str, dict[str, str]] = {}
    with psycopg.connect(postgres_url, connect_timeout=3) as db:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
            for table, col, dtype in cur.fetchall():
                out.setdefault(table, {})[col] = dtype
    return out
