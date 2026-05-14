"""Barrel export generator.

Walks a feature directory, identifies what should be exported, and writes
an `index.js` (or `index.ts`) that re-exports the public API. Includes
"internal vs public" comments based on cross-feature usage analysis when
the data is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scarlet.analyzer.metadata import (
    FeatureMetadata,
    extract_feature_metadata,
)


@dataclass(frozen=True)
class BarrelGenResult:
    """Result of a barrel generation pass."""

    feature_name: str
    barrel_path: str
    exported_count: int
    content: str
    written: bool

    def to_dict(self) -> dict:
        return {
            "feature_name": self.feature_name,
            "barrel_path": self.barrel_path,
            "exported_count": self.exported_count,
            "content": self.content,
            "written": self.written,
        }


def generate_barrel(
    feature_path: Path,
    extension: str = "js",
    write: bool = True,
) -> BarrelGenResult:
    """Generate a barrel export file for a feature.

    Args:
        feature_path: Absolute path to the feature directory.
        extension: File extension to use ("js", "ts", "tsx").
        write: If True, write the file. If False, return content only.

    Returns:
        BarrelGenResult with the generated content and metadata.
    """
    feature_path = feature_path.resolve()
    metadata = extract_feature_metadata(feature_path)

    content = _build_barrel_content(metadata, feature_path)
    barrel_path = feature_path / f"index.{extension}"

    if write:
        barrel_path.write_text(content, encoding="utf-8")

    return BarrelGenResult(
        feature_name=feature_path.name,
        barrel_path=str(barrel_path),
        exported_count=len(metadata.exports),
        content=content,
        written=write,
    )


def _build_barrel_content(metadata: FeatureMetadata, feature_path: Path) -> str:
    """Build the barrel export file content from metadata.

    Strategy: re-export every default-exported component, hook, and slice
    from files inside the feature. Files inside subdirectories named
    `components/`, `hooks/`, `slices/`, `api/` are considered candidates
    for the public API.
    """
    public_exports: list[str] = []

    # Group exports by category for clean ordering: components → hooks → slices → other
    components: list[tuple[str, str]] = []
    hooks: list[tuple[str, str]] = []
    slices: list[tuple[str, str]] = []
    types: list[tuple[str, str]] = []

    for symbol in metadata.exports:
        # Only include default exports OR named exports from "obvious" public files
        rel_path = _relative_import_path(Path(symbol.file_path), feature_path)
        if rel_path is None:
            continue

        match symbol.kind:
            case "component":
                if symbol.is_default_export:
                    components.append((symbol.name, rel_path))
            case "hook":
                hooks.append((symbol.name, rel_path))
            case "slice" | "api":
                slices.append((symbol.name, rel_path))
            case "interface" | "type":
                types.append((symbol.name, rel_path))

    lines: list[str] = []

    if components:
        lines.append("// Components")
        for name, path in components:
            lines.append(f'export {{ default as {name} }} from "{path}";')
        lines.append("")

    if hooks:
        lines.append("// Hooks")
        for name, path in hooks:
            lines.append(f'export {{ {name} }} from "{path}";')
        lines.append("")

    if slices:
        lines.append("// Store")
        for name, path in slices:
            lines.append(f'export {{ {name} }} from "{path}";')
        lines.append("")

    if types:
        lines.append("// Types")
        for name, path in types:
            lines.append(f'export type {{ {name} }} from "{path}";')
        lines.append("")

    if not lines:
        return f"// No public exports detected for feature: {feature_path.name}\n"

    return "\n".join(lines).rstrip() + "\n"


def _relative_import_path(file_path: Path, feature_root: Path) -> str | None:
    """Convert an absolute file path to a relative import string usable in a barrel.

    Strips the file extension and adds the leading `./`.
    """
    try:
        rel = file_path.relative_to(feature_root)
    except ValueError:
        return None

    # Strip extension
    rel_no_ext = rel.with_suffix("")
    return f"./{rel_no_ext.as_posix()}"
