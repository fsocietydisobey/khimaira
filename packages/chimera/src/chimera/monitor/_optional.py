"""Lazy-import helper for optional `chimera[monitor]` extras.

Every backend module that needs psycopg / fastapi / tree-sitter / uvicorn
imports them through `require()` so a missing extra surfaces as a clean
exit message instead of an import-time traceback at the entry point.
"""

import importlib
import sys
from types import ModuleType

_INSTALL_HINT = "Install monitor extras: uv pip install 'chimera[monitor]'"


def require(module_name: str) -> ModuleType:
    """Import `module_name` or exit with a clean install hint.

    Args:
        module_name: dotted module path (e.g. "psycopg", "fastapi").

    Returns:
        The imported module.

    Exits:
        With code 1 and a single-line message when the extra is missing.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        print(
            f"chimera monitor: missing dependency `{module_name}` ({exc}).\n{_INSTALL_HINT}",
            file=sys.stderr,
        )
        sys.exit(1)
