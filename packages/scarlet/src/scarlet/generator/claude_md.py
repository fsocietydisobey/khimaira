"""Feature CLAUDE.md generator.

Generates and updates per-feature CLAUDE.md files. The auto-derivable
sections (Public API, Key files, Consumers, See also) are refreshed every
time. The judgment sections (Vocabulary, Conventions, Common tasks,
Gotchas) are preserved across regenerations using <!-- BEGIN MANUAL -->
markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scarlet.analyzer.features import scan_features
from scarlet.analyzer.imports import build_feature_graph
from scarlet.analyzer.metadata import (
    FeatureMetadata,
    extract_feature_metadata,
)
from scarlet.config import load_config
from scarlet.generator.template import (
    DEFAULT_CLAUDE_MD_TEMPLATE,
    DEFAULT_DESCRIPTION_PLACEHOLDER,
)

# Regex for the manual-section markers we preserve on update
MANUAL_BLOCK_RE = re.compile(
    r"<!-- BEGIN MANUAL: (?P<key>[a-z_]+) -->\n?(?P<body>.*?)<!-- END MANUAL: (?P=key) -->",
    re.DOTALL,
)


@dataclass(frozen=True)
class ClaudeMdResult:
    """Result of a CLAUDE.md generation/update pass."""

    feature_name: str
    claude_md_path: str
    content: str
    written: bool
    is_new: bool

    def to_dict(self) -> dict:
        return {
            "feature_name": self.feature_name,
            "claude_md_path": self.claude_md_path,
            "content": self.content,
            "written": self.written,
            "is_new": self.is_new,
        }


def build_claude_md(
    project_path: Path,
    feature_path: Path,
    import_alias: str | None = None,
    write: bool = True,
) -> ClaudeMdResult:
    """Generate or update a feature CLAUDE.md.

    If a CLAUDE.md already exists, the manual sections are preserved and
    the auto sections are refreshed. If not, a fresh skeleton is written
    with placeholders in the manual sections.

    Args:
        project_path: Absolute path to the project root.
        feature_path: Absolute path to the feature directory.
        import_alias: The canonical import path for this feature
                      (e.g. "@/features/drawings"). Defaults to a guess.
        write: If True, write the file. If False, return content only.

    Returns:
        ClaudeMdResult with the new content and status.
    """
    project_path = project_path.resolve()
    feature_path = feature_path.resolve()

    metadata = extract_feature_metadata(feature_path)
    consumers = _find_consumers(project_path, feature_path.name)

    if import_alias is None:
        import_alias = f"@/features/{feature_path.name}"

    auto_sections = {
        "public_api": _render_public_api(metadata),
        "key_files": _render_key_files(metadata, feature_path),
        "consumers": _render_consumers(consumers),
        "see_also": _render_see_also(metadata),
    }

    claude_md_path = feature_path / "CLAUDE.md"
    is_new = not claude_md_path.exists()

    if is_new:
        # Fresh skeleton — manual sections start with placeholders
        manual_sections: dict[str, str] = {}
    else:
        # Preserve existing manual sections
        existing = claude_md_path.read_text(encoding="utf-8")
        manual_sections = _extract_manual_sections(existing)

    # Load project-level config and use its template if provided
    config = load_config(project_path)
    template = config.claude_md_template or DEFAULT_CLAUDE_MD_TEMPLATE

    content = _render_template(
        feature_name=feature_path.name,
        feature_display_name=_to_display_name(feature_path.name),
        import_alias=import_alias,
        auto_sections=auto_sections,
        manual_sections=manual_sections,
        template=template,
    )

    if write:
        claude_md_path.write_text(content, encoding="utf-8")

    return ClaudeMdResult(
        feature_name=feature_path.name,
        claude_md_path=str(claude_md_path),
        content=content,
        written=write,
        is_new=is_new,
    )


def _to_display_name(slug: str) -> str:
    """Convert a feature directory slug to a human-readable display name.

    Examples:
        'creation-flow' → 'Creation Flow'
        'client_detail' → 'Client Detail'
        'dashboard' → 'Dashboard'
    """
    return slug.replace("-", " ").replace("_", " ").title()


def _render_template(
    feature_name: str,
    import_alias: str,
    auto_sections: dict[str, str],
    manual_sections: dict[str, str],
    template: str = DEFAULT_CLAUDE_MD_TEMPLATE,
    feature_display_name: str | None = None,
) -> str:
    """Apply the template, filling auto sections and preserving manual content.

    `template` defaults to Scarlet's built-in template but can be overridden
    per-project via `.scarlet.yml` (`claude_md_template:`). Custom templates
    can include additional `<!-- BEGIN MANUAL: <key> -->` blocks for sections
    like Architecture, Data and state, Permissions — any manual content
    matching those keys in an existing CLAUDE.md is preserved on regenerate.

    Template variables:
        {feature_name}         — the directory slug (e.g., "creation-flow")
                                 — use for CLI references: `scarlet describe <name>`
        {feature_display_name} — title-cased human-readable name (e.g., "Creation Flow")
                                 — use for the H1 heading
        {import_alias}         — canonical import path
        {description_placeholder}, {public_api}, {key_files},
        {consumers}, {see_also}, {timestamp}
    """
    if feature_display_name is None:
        feature_display_name = _to_display_name(feature_name)
    content = template.format(
        feature_name=feature_name,
        feature_display_name=feature_display_name,
        import_alias=import_alias,
        description_placeholder=manual_sections.get(
            "description", DEFAULT_DESCRIPTION_PLACEHOLDER
        ),
        public_api=auto_sections["public_api"],
        key_files=auto_sections["key_files"],
        consumers=auto_sections["consumers"],
        see_also=auto_sections["see_also"],
        timestamp=datetime.now().strftime("%Y-%m-%d"),
    )

    # If we have preserved manual sections (vocabulary, conventions, etc.),
    # splice them back in over the placeholders.
    for key, body in manual_sections.items():
        if key == "description":
            continue  # already handled in format()
        marker_start = f"<!-- BEGIN MANUAL: {key} -->"
        marker_end = f"<!-- END MANUAL: {key} -->"
        pattern = re.compile(
            re.escape(marker_start) + r"\n?.*?" + re.escape(marker_end), re.DOTALL
        )
        content = pattern.sub(f"{marker_start}\n{body.rstrip()}\n{marker_end}", content)

    return content


def _extract_manual_sections(content: str) -> dict[str, str]:
    """Pull all <!-- BEGIN MANUAL: X --> ... <!-- END MANUAL: X --> blocks from existing content."""
    sections: dict[str, str] = {}
    for match in MANUAL_BLOCK_RE.finditer(content):
        sections[match.group("key")] = match.group("body").strip()
    return sections


def _render_public_api(metadata: FeatureMetadata) -> str:
    """Render the Public API section from feature metadata.

    When multiple exports share the same name (e.g. a modern `Dashboard` and a
    `DashboardLegacy/Dashboard`), the feature-relative path is shown instead of
    the bare filename to disambiguate.
    """
    feature_path = Path(metadata.path)
    components = [e for e in metadata.exports if e.kind == "component"]
    hooks = [e for e in metadata.exports if e.kind == "hook"]
    slices = [e for e in metadata.exports if e.kind in ("slice", "api")]
    constants = [e for e in metadata.exports if e.kind == "constant"]
    types = [e for e in metadata.exports if e.kind in ("interface", "type")]

    name_counts: dict[str, int] = {}
    for export in metadata.exports:
        name_counts[export.name] = name_counts.get(export.name, 0) + 1

    def _display_path(export: ExportedSymbol) -> str:
        file = Path(export.file_path)
        if name_counts.get(export.name, 0) > 1:
            try:
                return str(file.relative_to(feature_path))
            except ValueError:
                return file.name
        return file.name

    lines: list[str] = []

    if components:
        lines.append("**Components:**")
        for c in sorted(components, key=lambda e: (e.name, e.file_path)):
            lines.append(f"- `{c.name}` — {_display_path(c)}")
        lines.append("")

    if hooks:
        lines.append("**Hooks:**")
        for h in sorted(hooks, key=lambda e: (e.name, e.file_path)):
            lines.append(f"- `{h.name}()` — {_display_path(h)}")
        lines.append("")

    if slices:
        lines.append("**Store / API:**")
        for s in sorted(slices, key=lambda e: (e.name, e.file_path)):
            lines.append(f"- `{s.name}` — {_display_path(s)}")
        lines.append("")

    if constants:
        lines.append("**Constants:**")
        for c in sorted(constants, key=lambda e: (e.name, e.file_path)):
            lines.append(f"- `{c.name}` — {_display_path(c)}")
        lines.append("")

    if types:
        lines.append("**Types:**")
        for t in sorted(types, key=lambda e: (e.name, e.file_path)):
            lines.append(f"- `{t.name}` — {_display_path(t)}")
        lines.append("")

    if not lines:
        return "_No public exports detected. Did you mean to add a barrel export (`index.js`)?_\n"

    return "\n".join(lines).rstrip() + "\n"


def _render_key_files(metadata: FeatureMetadata, feature_path: Path) -> str:
    """Render the Key files section."""
    if not metadata.exports:
        return "_No source files detected._\n"

    # Group exports by file, then list files with the symbols they contain
    by_file: dict[str, list[str]] = {}
    for export in metadata.exports:
        rel = Path(export.file_path).relative_to(feature_path)
        by_file.setdefault(str(rel), []).append(f"`{export.name}` ({export.kind})")

    lines: list[str] = []
    for file in sorted(by_file.keys()):
        symbols = ", ".join(by_file[file])
        lines.append(f"- `{file}` — {symbols}")

    return "\n".join(lines) + "\n"


def _render_consumers(consumers: list[tuple[str, list[str]]]) -> str:
    """Render the Consumers section.

    consumers: list of (consumer_feature_name, list_of_files)
    """
    if not consumers:
        return "_No cross-feature consumers detected._\n"

    lines: list[str] = []
    for consumer, files in sorted(consumers):
        lines.append(f"- **{consumer}** — imports from this feature")
        for f in files[:3]:  # Cap at 3 to keep it tidy
            lines.append(f"  - `{f}`")
        if len(files) > 3:
            lines.append(f"  - _...and {len(files) - 3} more_")
    return "\n".join(lines) + "\n"


def _render_see_also(metadata: FeatureMetadata) -> str:
    """Render the See also section. Auto-suggests global rule files."""
    lines = [
        "- `.claude/rules/frontend.md` — auto-loaded global frontend rules.",
    ]
    return "\n".join(lines) + "\n"


def _find_consumers(project_path: Path, feature_name: str) -> list[tuple[str, list[str]]]:
    """Find all features that import from the given feature.

    Returns a list of (consumer_feature_name, list_of_files).
    Files are returned as paths relative to the consumer feature root.
    """
    graph = build_feature_graph(project_path)

    consumers_by_feature: dict[str, list[str]] = {}

    for (from_feature, to_feature), files in graph.edge_files.items():
        if to_feature != feature_name:
            continue

        # Make file paths relative to the project root for readability
        rel_files: list[str] = []
        for f in files:
            try:
                rel = Path(f).relative_to(project_path)
                rel_files.append(str(rel))
            except ValueError:
                rel_files.append(f)

        consumers_by_feature[from_feature] = sorted(rel_files)

    return sorted(consumers_by_feature.items())
