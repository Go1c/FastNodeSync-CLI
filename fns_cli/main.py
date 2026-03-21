"""CLI entry point: run / sync / push / pull / status commands."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path

import click

from . import __version__
from .config import load_config
from .logger import setup_logger
from .state import SyncState
from .sync_engine import SyncEngine


def _run_async(coro):
    """Run an async coroutine with graceful Ctrl+C handling."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()

    main_task = loop.create_task(coro)

    def _shutdown():
        main_task.cancel()

    try:
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGINT, _shutdown)
            loop.add_signal_handler(signal.SIGTERM, _shutdown)
    except NotImplementedError:
        pass

    try:
        loop.run_until_complete(main_task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


@click.group()
@click.version_option(__version__, prog_name="fns-cli")
def cli():
    """FastNodeSync CLI - sync Obsidian vaults from the command line."""


@cli.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config.yaml")
def run(config_path: str):
    """Start continuous sync (watch + push + pull)."""
    cfg = load_config(config_path)
    setup_logger(cfg.logging.level, cfg.logging.file)
    engine = SyncEngine(cfg)
    click.echo(f"FastNodeSync CLI v{__version__}")
    click.echo(f"  Server : {cfg.server.api}")
    click.echo(f"  Vault  : {cfg.server.vault}")
    click.echo(f"  Path   : {cfg.vault_path}")
    click.echo()
    _run_async(engine.run())


@cli.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config.yaml")
def sync(config_path: str):
    """Run a full bidirectional sync, then exit."""
    cfg = load_config(config_path)
    setup_logger(cfg.logging.level, cfg.logging.file)
    engine = SyncEngine(cfg)
    click.echo("Running full sync...")
    _run_async(engine.sync_once())
    click.echo("Sync complete.")


@cli.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config.yaml")
def pull(config_path: str):
    """Pull remote changes to local vault."""
    cfg = load_config(config_path)
    setup_logger(cfg.logging.level, cfg.logging.file)
    engine = SyncEngine(cfg)
    click.echo("Pulling remote changes...")
    _run_async(engine.pull())
    click.echo("Pull complete.")


@cli.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config.yaml")
def push(config_path: str):
    """Push all local files to remote."""
    cfg = load_config(config_path)
    setup_logger(cfg.logging.level, cfg.logging.file)
    engine = SyncEngine(cfg)
    click.echo("Pushing local files...")
    _run_async(engine.push())
    click.echo("Push complete.")


@cli.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config.yaml")
def status(config_path: str):
    """Show sync state and configuration."""
    cfg = load_config(config_path)
    state = SyncState.load(cfg.vault_path)

    click.echo(f"FastNodeSync CLI v{__version__}")
    click.echo()
    click.echo("Configuration:")
    click.echo(f"  Server       : {cfg.server.api}")
    click.echo(f"  Vault        : {cfg.server.vault}")
    click.echo(f"  Watch path   : {cfg.vault_path}")
    click.echo(f"  Sync notes   : {cfg.sync.sync_notes}")
    click.echo(f"  Sync files   : {cfg.sync.sync_files}")
    click.echo(f"  Sync config  : {cfg.sync.sync_config}")
    click.echo()
    click.echo("Sync state:")
    click.echo(f"  Last note sync : {state.last_note_sync_time}")
    click.echo(f"  Last file sync : {state.last_file_sync_time}")

    md_count = sum(1 for _ in cfg.vault_path.rglob("*.md")) if cfg.vault_path.exists() else 0
    total = sum(1 for _ in cfg.vault_path.rglob("*") if _.is_file()) if cfg.vault_path.exists() else 0
    click.echo()
    click.echo("Local vault:")
    click.echo(f"  Notes (.md)  : {md_count}")
    click.echo(f"  Total files  : {total}")


def main():
    cli()


if __name__ == "__main__":
    main()
