"""CLI entry point for Scarlet.

Commands:
  scarlet scan <project>             Survey project, return structural overview
  scarlet describe <project> <feature>  Generate or refresh one CLAUDE.md
  scarlet sync <project>             Refresh all feature CLAUDE.md files
  scarlet barrel <project> <feature>  Generate index.js for a feature
  scarlet graph <project>            Generate Mermaid dependency graph
  scarlet lint <project>             Validate all CLAUDE.md files
  scarlet serve                      Start the MCP server (stdio)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
def main(verbose: bool) -> None:
    """Scarlet — codebase cartographer."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def scan(path: str, as_json: bool) -> None:
    """Scan a project: detect framework, list features, summarize state."""
    from scarlet.analyzer.features import scan_features
    from scarlet.analyzer.project import analyze_project

    project_path = Path(path).resolve()
    manifest = analyze_project(project_path)
    summaries = scan_features(project_path)

    if as_json:
        output = {
            "manifest": manifest.to_dict(),
            "features": [s.to_dict() for s in summaries],
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"Project:           {manifest.path}")
    click.echo(f"Type:              {manifest.project_type}")
    click.echo(f"TypeScript:        {manifest.has_typescript}")
    click.echo(f"State management:  {manifest.state_management or '—'}")
    click.echo(f"Test framework:    {manifest.test_framework or '—'}")
    click.echo(f"Package manager:   {manifest.package_manager or '—'}")
    click.echo(f"Features root:     {manifest.features_root}")
    click.echo(f"Feature count:     {manifest.feature_count}")
    click.echo()

    if not summaries:
        click.echo("No features detected.")
        return

    click.echo(f"{'Feature':<25} {'CLAUDE.md':<12} {'Barrel':<10} {'Components':<12} {'Hooks':<8} {'Slices':<8}")
    click.echo("─" * 75)
    for s in summaries:
        click.echo(
            f"{s.name:<25} "
            f"{'✓' if s.has_claude_md else '✗':<12} "
            f"{'✓' if s.has_barrel else '✗':<10} "
            f"{s.component_count:<12} "
            f"{s.hook_count:<8} "
            f"{s.slice_count:<8}"
        )


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("feature_name")
@click.option("--alias", default=None, help="Canonical import alias (e.g. @/features/X).")
@click.option("--dry-run", is_flag=True, help="Print the generated content instead of writing.")
def describe(project_path: str, feature_name: str, alias: str | None, dry_run: bool) -> None:
    """Generate or refresh a feature's CLAUDE.md."""
    from scarlet.analyzer.features import scan_features
    from scarlet.generator.claude_md import build_claude_md

    pp = Path(project_path).resolve()
    summaries = scan_features(pp)
    target = next((s for s in summaries if s.name == feature_name), None)

    if not target:
        click.echo(f"Feature '{feature_name}' not found.", err=True)
        sys.exit(1)

    result = build_claude_md(
        pp, Path(target.path), import_alias=alias, write=not dry_run
    )

    if dry_run:
        click.echo(result.content)
    else:
        action = "Created" if result.is_new else "Updated"
        click.echo(f"{action} {result.claude_md_path}")


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.option("--alias-prefix", default="@/features/", help="Import alias prefix.")
def sync(project_path: str, alias_prefix: str) -> None:
    """Refresh CLAUDE.md for every feature in a project."""
    from scarlet.analyzer.features import scan_features
    from scarlet.generator.claude_md import build_claude_md

    pp = Path(project_path).resolve()
    summaries = scan_features(pp)

    if not summaries:
        click.echo("No features found.")
        return

    created = 0
    updated = 0
    for s in summaries:
        result = build_claude_md(
            pp, Path(s.path), import_alias=f"{alias_prefix}{s.name}"
        )
        if result.is_new:
            created += 1
            click.echo(f"  + {s.name}")
        else:
            updated += 1
            click.echo(f"  ~ {s.name}")

    click.echo()
    click.echo(f"Created: {created}, Updated: {updated}")


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("feature_name")
@click.option("--ext", "extension", default="js", help="Barrel file extension (js/ts/tsx).")
@click.option("--dry-run", is_flag=True, help="Print the generated content instead of writing.")
def barrel(project_path: str, feature_name: str, extension: str, dry_run: bool) -> None:
    """Generate a barrel export file (index.js) for a feature."""
    from scarlet.analyzer.features import scan_features
    from scarlet.generator.barrel import generate_barrel

    pp = Path(project_path).resolve()
    summaries = scan_features(pp)
    target = next((s for s in summaries if s.name == feature_name), None)

    if not target:
        click.echo(f"Feature '{feature_name}' not found.", err=True)
        sys.exit(1)

    result = generate_barrel(Path(target.path), extension=extension, write=not dry_run)

    if dry_run:
        click.echo(result.content)
    else:
        click.echo(f"Wrote {result.barrel_path} ({result.exported_count} exports)")


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.option("--format", "output_format", default="mermaid", type=click.Choice(["mermaid", "json"]))
@click.option("--output", "-o", default=None, help="Write to file instead of stdout.")
def graph(project_path: str, output_format: str, output: str | None) -> None:
    """Generate a feature-level dependency graph (Mermaid or JSON)."""
    from scarlet.generator.dep_graph import generate_dep_graph

    pp = Path(project_path).resolve()
    result = generate_dep_graph(pp, format=output_format)

    if output:
        Path(output).write_text(result.content, encoding="utf-8")
        click.echo(f"Wrote {output} ({result.feature_count} features, {result.edge_count} edges)")
    else:
        click.echo(result.content)


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
def lint(project_path: str) -> None:
    """Lint all feature CLAUDE.md files for staleness and missing sections."""
    from scarlet.analyzer.features import scan_features
    from scarlet.validator.linter import lint_feature_claude_md

    pp = Path(project_path).resolve()
    summaries = scan_features(pp)

    total_errors = 0
    total_warnings = 0

    for s in summaries:
        report = lint_feature_claude_md(Path(s.path))
        if not report.issues:
            continue
        click.echo(f"\n{s.name}:")
        for issue in report.issues:
            symbol = {"error": "✗", "warning": "⚠", "info": "ℹ"}[issue.level.value]
            click.echo(f"  {symbol} [{issue.code}] {issue.message}")
        total_errors += report.error_count
        total_warnings += report.warning_count

    click.echo()
    click.echo(f"Total: {total_errors} errors, {total_warnings} warnings")


@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("feature_name")
def invariants(project_path: str, feature_name: str) -> None:
    """Scan a feature for invariant candidates (warnings, magic numbers, TODOs)."""
    from scarlet.analyzer.features import scan_features
    from scarlet.analyzer.invariants import extract_invariants

    pp = Path(project_path).resolve()
    summaries = scan_features(pp)
    target = next((s for s in summaries if s.name == feature_name), None)

    if not target:
        click.echo(f"Feature '{feature_name}' not found.", err=True)
        sys.exit(1)

    report = extract_invariants(Path(target.path))

    if report.total_count == 0:
        click.echo(f"No invariant candidates found in {feature_name}.")
        return

    click.echo(f"Invariant candidates for '{feature_name}':\n")

    if report.warnings:
        click.echo(f"Warnings ({len(report.warnings)}):")
        for inv in report.warnings:
            click.echo(f"  {inv.file}:{inv.line}  {inv.text}")
        click.echo()

    if report.intentional:
        click.echo(f"Intentional callouts ({len(report.intentional)}):")
        for inv in report.intentional:
            click.echo(f"  {inv.file}:{inv.line}  {inv.text}")
        click.echo()

    if report.magic_numbers:
        click.echo(f"Magic numbers ({len(report.magic_numbers)}):")
        for inv in report.magic_numbers:
            click.echo(f"  {inv.file}:{inv.line}  {inv.text}")
        click.echo()

    if report.todos:
        click.echo(f"TODOs ({len(report.todos)}):")
        for inv in report.todos:
            click.echo(f"  {inv.file}:{inv.line}  {inv.text}")


@main.command()
def serve() -> None:
    """Start the Scarlet MCP server (stdio transport)."""
    from scarlet.server import mcp

    mcp.run()
