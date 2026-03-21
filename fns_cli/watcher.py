"""File system watcher: watchdog integration, debounce, anti-loop, exclusions."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.watcher")

DEBOUNCE_SECONDS = 0.5


class _VaultEventHandler(FileSystemEventHandler):
    """Translates watchdog FS events into async calls on the SyncEngine."""

    def __init__(self, engine: SyncEngine, loop: asyncio.AbstractEventLoop) -> None:
        self.engine = engine
        self.loop = loop
        self._pending: dict[str, asyncio.TimerHandle] = {}

    def _rel(self, abs_path: str) -> str:
        return Path(abs_path).relative_to(self.engine.vault_path).as_posix()

    def _schedule(self, key: str, coro_factory):
        handle = self._pending.pop(key, None)
        if handle:
            handle.cancel()

        def _fire():
            self._pending.pop(key, None)
            asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)

        self._pending[key] = self.loop.call_later(DEBOUNCE_SECONDS, _fire)

    # ── watchdog callbacks (called from observer thread) ─────────────

    def on_created(self, event):
        if event.is_directory:
            return
        try:
            rel = self._rel(event.src_path)
        except ValueError:
            return
        if self.engine.is_ignored(rel) or self.engine.is_excluded(rel):
            return
        self._schedule(f"mod:{rel}", lambda: self.engine.on_local_change(rel))

    def on_modified(self, event):
        if event.is_directory:
            return
        try:
            rel = self._rel(event.src_path)
        except ValueError:
            return
        if self.engine.is_ignored(rel) or self.engine.is_excluded(rel):
            return
        self._schedule(f"mod:{rel}", lambda: self.engine.on_local_change(rel))

    def on_deleted(self, event):
        if event.is_directory:
            return
        try:
            rel = self._rel(event.src_path)
        except ValueError:
            return
        if self.engine.is_ignored(rel) or self.engine.is_excluded(rel):
            return
        self._schedule(f"del:{rel}", lambda: self.engine.on_local_delete(rel))

    def on_moved(self, event):
        if event.is_directory:
            return
        try:
            old_rel = self._rel(event.src_path)
            new_rel = self._rel(event.dest_path)
        except ValueError:
            return
        if self.engine.is_excluded(new_rel):
            return
        self._schedule(
            f"mv:{old_rel}:{new_rel}",
            lambda: self.engine.on_local_rename(new_rel, old_rel),
        )


class VaultWatcher:
    """Wraps watchdog Observer to monitor the vault directory."""

    def __init__(self, engine: SyncEngine, loop: asyncio.AbstractEventLoop) -> None:
        self.engine = engine
        self._observer = Observer()
        self._handler = _VaultEventHandler(engine, loop)
        self._watching = False

    def start(self) -> None:
        path = str(self.engine.vault_path)
        log.info("Starting file watcher on %s", path)
        self._observer.schedule(self._handler, path, recursive=True)
        self._observer.start()
        self._watching = True

    def stop(self) -> None:
        if self._watching:
            log.info("Stopping file watcher")
            self._observer.stop()
            self._observer.join(timeout=5)
            self._watching = False
