"""CLI entry point for Specter.

Commands:
  specter status         Check if Firefox is reachable
  specter logs           Print recent console logs
  specter errors         Print recent JS exceptions
  specter network        Print recent network activity
  specter screenshot     Take a screenshot
  specter serve          Start the MCP server (stdio)
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from specter.config import load_config


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
def main(verbose: bool) -> None:
    """Specter — browser debugging for AI assistants."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


@main.command()
def status() -> None:
    """Check if Firefox is reachable on the debug port."""
    from specter.browser.connection import CDPConnection

    config = load_config()
    conn = CDPConnection(config)

    async def check():
        try:
            targets = await conn.list_targets()
            click.echo(f"Firefox reachable at {config.http_endpoint}")
            click.echo(f"Open tabs: {len(targets)}")
            for t in targets:
                click.echo(f"  [{t.id[:8]}] {t.title} — {t.url}")
        except ConnectionError as e:
            click.echo(f"Cannot connect: {e}", err=True)
            sys.exit(1)

    asyncio.run(check())


@main.command()
@click.option("--level", "-l", default=None, help="Filter by level (log/warn/error/info).")
@click.option("--limit", "-n", default=20, help="Max entries.")
def logs(level: str | None, limit: int) -> None:
    """Print recent console logs."""
    from specter.browser.connection import CDPConnection
    from specter.browser.console import ConsoleCapture

    config = load_config()

    async def run():
        conn = CDPConnection(config)
        console = ConsoleCapture(config)
        console.register(conn)

        try:
            await conn.connect()
            await console.enable(conn)
        except ConnectionError as e:
            click.echo(f"Cannot connect: {e}", err=True)
            sys.exit(1)

        # Capture for a couple seconds to collect buffered events
        click.echo("Capturing console output (2s)...")
        await asyncio.sleep(2)

        entries = console.get_logs(level=level, limit=limit)
        await conn.disconnect()

        if not entries:
            click.echo("No console entries captured.")
            return

        for entry in entries:
            lvl = entry["level"].upper()
            text = entry["text"][:200]
            source = entry.get("source", "")
            click.echo(f"  [{lvl}] {text}")
            if source:
                click.echo(f"         {source}")

    asyncio.run(run())


@main.command()
@click.option("--full", is_flag=True, help="Capture the full scrollable page.")
@click.option("--selector", "-s", default=None, help="CSS selector to screenshot.")
def screenshot(full: bool, selector: str | None) -> None:
    """Take a screenshot of the current page."""
    from specter.browser.connection import CDPConnection
    from specter.browser.runtime import Runtime

    config = load_config()

    async def run():
        conn = CDPConnection(config)
        runtime = Runtime(config)

        try:
            await conn.connect()
        except ConnectionError as e:
            click.echo(f"Cannot connect: {e}", err=True)
            sys.exit(1)

        result = await runtime.take_screenshot(conn, full_page=full, selector=selector)
        await conn.disconnect()

        if "error" in result:
            click.echo(f"Error: {result['error']}", err=True)
            sys.exit(1)

        click.echo(f"Screenshot saved: {result['file_path']}")

    asyncio.run(run())


@main.command()
def serve() -> None:
    """Start the Specter MCP server (stdio transport)."""
    from specter.server import mcp

    mcp.run()
