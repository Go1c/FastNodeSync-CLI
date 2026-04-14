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
        self._known_files: set[str] = set()
        self._seed_known_files()

    def _rel(self, abs_path: str) -> str:
        return Path(abs_path).relative_to(self.engine.vault_path).as_posix()

    def _rel_or_none(self, abs_path: str) -> str | None:
        try:
            return self._rel(abs_path)
        except ValueError:
            return None

    def _schedule(self, key: str, coro_factory):
        handle = self._pending.pop(key, None)
        if handle:
            handle.cancel()

        def _fire():
            self._pending.pop(key, None)
            asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)

        self._pending[key] = self.loop.call_later(DEBOUNCE_SECONDS, _fire)

    def _seed_known_files(self) -> None:
        for fp in self.engine.vault_path.rglob("*"):
            if not fp.is_file():
                continue
            rel = fp.relative_to(self.engine.vault_path).as_posix()
            if self.engine.is_excluded(rel):
                continue
            self._known_files.add(rel)

    def _track_file(self, rel_path: str) -> None:
        if not self.engine.is_excluded(rel_path):
            self._known_files.add(rel_path)

    def _untrack_file(self, rel_path: str) -> None:
        self._known_files.discard(rel_path)

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
        self._track_file(rel)
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
        self._track_file(rel)
        self._schedule(f"mod:{rel}", lambda: self.engine.on_local_change(rel))

    def on_deleted(self, event):
        if event.is_directory:
            self._handle_directory_delete(event)
            return
        try:
            rel = self._rel(event.src_path)
        except ValueError:
            return
        if self.engine.is_ignored(rel) or self.engine.is_excluded(rel):
            return
        self._untrack_file(rel)
        self._schedule(f"del:{rel}", lambda: self.engine.on_local_delete(rel))

    def on_moved(self, event):
        if event.is_directory:
            # On Windows (and commonly on macOS) a directory rename is a
            # single atomic event — watchdog does NOT emit child events —
            # so if we ignore it the server never learns about the rename.
            # Enumerate the new path's files and emit a rename for each,
            # computing each old path by swapping the renamed prefix.
            self._handle_directory_move(event)
            return
        old_rel = self._rel_or_none(event.src_path)
        new_rel = self._rel_or_none(event.dest_path)
        if old_rel is None and new_rel is None:
            return
        if old_rel is None:
            if self.engine.is_ignored(new_rel) or self.engine.is_excluded(new_rel):
                return
            self._track_file(new_rel)
            self._schedule(f"mod:{new_rel}", lambda n=new_rel: self.engine.on_local_change(n))
            return
        if new_rel is None:
            if self.engine.is_ignored(old_rel) or self.engine.is_excluded(old_rel):
                return
            self._untrack_file(old_rel)
            self._schedule(f"del:{old_rel}", lambda o=old_rel: self.engine.on_local_delete(o))
            return
        self._schedule_move_transition(old_rel, new_rel)

    def _handle_directory_delete(self, event) -> None:
        rel_dir = self._rel_or_none(event.src_path)
        if rel_dir is None:
            return
        prefix = f"{rel_dir}/"
        victims = sorted(
            rel for rel in self._known_files
            if rel == rel_dir or rel.startswith(prefix)
        )
        for rel in victims:
            if self.engine.is_ignored(rel) or self.engine.is_excluded(rel):
                continue
            self._untrack_file(rel)
            self._schedule(f"del:{rel}", lambda p=rel: self.engine.on_local_delete(p))

    def _handle_directory_move(self, event) -> None:
        """Enumerate a renamed directory's children and schedule per-file renames."""
        new_dir = Path(event.dest_path)
        try:
            old_dir_rel = self._rel(event.src_path)
            new_dir_rel = self._rel(event.dest_path)
        except ValueError:
            return
        if not new_dir.exists():
            return
        for child in new_dir.rglob("*"):
            if not child.is_file():
                continue
            try:
                new_rel = self._rel(str(child))
            except ValueError:
                continue
            # child sits under new_dir_rel/...; compute old_rel by replacing
            # the directory prefix.
            if not new_rel.startswith(new_dir_rel + "/"):
                continue
            tail = new_rel[len(new_dir_rel) + 1:]
            old_rel = f"{old_dir_rel}/{tail}" if old_dir_rel else tail
            self._schedule_move_transition(old_rel, new_rel)

    def _schedule_move_transition(self, old_rel: str, new_rel: str) -> None:
        if self.engine.is_ignored(old_rel) or self.engine.is_ignored(new_rel):
            return

        old_excluded = self.engine.is_excluded(old_rel)
        new_excluded = self.engine.is_excluded(new_rel)

        if old_excluded and new_excluded:
            self._untrack_file(old_rel)
            return
        if not old_excluded and new_excluded:
            self._untrack_file(old_rel)
            self._schedule(
                f"del:{old_rel}",
                lambda o=old_rel: self.engine.on_local_delete(o),
            )
            return
        if old_excluded and not new_excluded:
            self._track_file(new_rel)
            self._schedule(
                f"mod:{new_rel}",
                lambda n=new_rel: self.engine.on_local_change(n),
            )
            return

        self._untrack_file(old_rel)
        self._track_file(new_rel)
        self._schedule(
            f"mv:{old_rel}:{new_rel}",
            lambda o=old_rel, n=new_rel: self.engine.on_local_rename(n, o),
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
