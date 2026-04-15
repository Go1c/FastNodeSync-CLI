"""Sync engine: coordinates NoteSync + FileSync + Watcher."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path

from .client import WSClient
from .config import AppConfig
from .file_sync import FileSync
from .folder_sync import FolderSync
from .note_sync import NoteSync
from .setting_sync import SettingSync
from .state import SyncState

log = logging.getLogger("fns_cli.sync_engine")


class SyncEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.vault_path = config.vault_path
        self.ws_client = WSClient(config)
        self.state = SyncState.load(self.vault_path)
        self.note_sync = NoteSync(self)
        self.file_sync = FileSync(self)
        self.folder_sync = FolderSync(self)
        self.setting_sync = SettingSync(self)
        self._ignored_files: set[str] = set()
        self._watch_enabled = False

    def ignore_file(self, rel_path: str) -> None:
        self._ignored_files.add(rel_path)

    def unignore_file(self, rel_path: str) -> None:
        self._ignored_files.discard(rel_path)

    def is_ignored(self, rel_path: str) -> bool:
        return rel_path in self._ignored_files

    def is_excluded(self, rel_path: str) -> bool:
        if rel_path == ".fns_state.json":
            return True
        for pattern in self.config.sync.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def _is_note(self, rel_path: str) -> bool:
        return rel_path.endswith(".md")

    def _is_config(self, rel_path: str) -> bool:
        first = rel_path.split("/")[0]
        return first.startswith(".")

    def _should_sync_file(self, rel_path: str) -> bool:
        if self._is_config(rel_path):
            return self.config.sync.sync_config
        if self._is_note(rel_path):
            return self.config.sync.sync_notes
        return self.config.sync.sync_files

    # ── Local change callbacks (from watcher) ────────────────────────

    async def on_local_change(self, rel_path: str) -> None:
        if not self._watch_enabled or not self._should_sync_file(rel_path):
            return
        if self._is_config(rel_path):
            await self.setting_sync.push_modify(rel_path)
        elif self._is_note(rel_path):
            await self.note_sync.push_modify(rel_path)
        else:
            await self.file_sync.push_upload(rel_path)

    async def on_local_delete(self, rel_path: str) -> None:
        if not self._watch_enabled or not self._should_sync_file(rel_path):
            return
        if self._is_config(rel_path):
            await self.setting_sync.push_delete(rel_path)
        elif self._is_note(rel_path):
            await self.note_sync.push_delete(rel_path)
        else:
            await self.file_sync.push_delete(rel_path)

    async def on_local_rename(self, new_rel: str, old_rel: str) -> None:
        if not self._watch_enabled:
            return
        if self._is_config(new_rel):
            await self.setting_sync.push_rename(new_rel, old_rel)
        elif self._is_note(new_rel):
            await self.note_sync.push_rename(new_rel, old_rel)
        else:
            await self.file_sync.push_delete(old_rel)
            await self.file_sync.push_upload(new_rel)

    # ── High-level sync operations ───────────────────────────────────

    def _register_handlers(self) -> None:
        self.note_sync.register_handlers()
        self.file_sync.register_handlers()
        self.folder_sync.register_handlers()
        self.setting_sync.register_handlers()

    async def run(self) -> None:
        """Connect, do initial sync, then watch for changes indefinitely."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self._register_handlers()

        loop = asyncio.get_running_loop()

        from .watcher import VaultWatcher
        watcher = VaultWatcher(self, loop)

        async def _on_reconnect() -> None:
            log.info("Reconnected — re-syncing")
            self._watch_enabled = False
            await self._initial_sync()
            self._watch_enabled = True

        self.ws_client.on_reconnect(_on_reconnect)

        ws_task = asyncio.create_task(self.ws_client.run())

        try:
            if not await self.ws_client.wait_ready(timeout=30):
                log.error("Failed to authenticate within 30s")
                return

            await self._initial_sync()

            self._watch_enabled = True
            watcher.start()
            log.info("Watching for local changes... (Ctrl+C to stop)")

            await ws_task
        except asyncio.CancelledError:
            pass
        finally:
            self._watch_enabled = False
            watcher.stop()
            await self.ws_client.close()

    async def sync_once(self) -> None:
        """Connect, do a full sync, then disconnect."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self._register_handlers()

        ws_task = asyncio.create_task(self.ws_client.run())

        try:
            if not await self.ws_client.wait_ready(timeout=30):
                log.error("Failed to authenticate within 30s")
                return

            if self.config.sync.sync_notes:
                await self.note_sync.request_full_sync()
                await self._wait_note_sync(timeout=120)

            if self.config.sync.sync_files or self.config.sync.sync_config:
                await self.file_sync.request_sync()
                await self._wait_file_sync(timeout=300)

            if self.config.sync.sync_config:
                await self.setting_sync.request_full_sync()
                await self._wait_setting_sync(timeout=120)
        finally:
            await self.ws_client.close()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

    async def pull(self) -> None:
        """Pull remote changes only."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self._register_handlers()

        ws_task = asyncio.create_task(self.ws_client.run())

        try:
            if not await self.ws_client.wait_ready(timeout=30):
                log.error("Failed to authenticate within 30s")
                return

            await self._initial_sync()
        finally:
            await self.ws_client.close()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

    async def push(self) -> None:
        """Push all local files to remote."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self._register_handlers()

        ws_task = asyncio.create_task(self.ws_client.run())

        try:
            if not await self.ws_client.wait_ready(timeout=30):
                log.error("Failed to authenticate within 30s")
                return

            if self.config.sync.sync_notes:
                await self.note_sync.request_full_sync()
                await self._wait_note_sync(timeout=120)

            if self.config.sync.sync_files:
                await self._push_all_files()

            if self.config.sync.sync_config:
                await self._push_all_settings()
        finally:
            await self.ws_client.close()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

    # ── Internal helpers ─────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        if self.config.sync.sync_notes:
            await self.note_sync.request_sync()
            await self._wait_note_sync(timeout=300)

        if self.config.sync.sync_files or self.config.sync.sync_config:
            await self.file_sync.request_sync()
            await self._wait_file_sync(timeout=300)

        if self.config.sync.sync_config:
            await self.setting_sync.request_sync()
            await self._wait_setting_sync(timeout=300)

    async def _wait_note_sync(self, timeout: float = 60) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.note_sync.is_sync_complete:
            if loop.time() > deadline:
                log.warning("NoteSync timed out after %.0fs", timeout)
                break
            await asyncio.sleep(0.5)

    async def _wait_file_sync(self, timeout: float = 60) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.file_sync.is_sync_complete:
            if self.file_sync.is_stalled(stale_seconds=5):
                log.warning(
                    "FileSync stalled after FileSyncEnd "
                    "(expected=%d, received=%d); continuing with watcher enabled",
                    self.file_sync._expected_modify + self.file_sync._expected_delete,
                    self.file_sync._received_modify + self.file_sync._received_delete,
                )
                break
            if loop.time() > deadline:
                log.warning("FileSync timed out after %.0fs", timeout)
                break
            await asyncio.sleep(0.5)
        # After FileSyncEnd, wait for any in-flight chunk downloads to finish
        dl_deadline = loop.time() + timeout
        while self.file_sync._download_sessions:
            if loop.time() > dl_deadline:
                log.warning(
                    "FileSync downloads timed out with %d sessions pending",
                    len(self.file_sync._download_sessions),
                )
                break
            await asyncio.sleep(0.5)

    async def _wait_setting_sync(self, timeout: float = 60) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.setting_sync.is_sync_complete:
            if loop.time() > deadline:
                log.warning("SettingSync timed out after %.0fs", timeout)
                break
            await asyncio.sleep(0.5)

    async def _push_all_files(self) -> None:
        """Upload every non-note, non-excluded file in the vault."""
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.is_excluded(rel) or rel.endswith(".md"):
                continue
            if not self._is_config(rel) and not self.config.sync.sync_files:
                continue
            if self._is_config(rel) and not self.config.sync.sync_config:
                continue
            await self.file_sync.push_upload(rel)
            await asyncio.sleep(0.05)

    async def _push_all_settings(self) -> None:
        """Upload every config file in dot-prefixed directories."""
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.is_excluded(rel) or not self._is_config(rel):
                continue
            await self.setting_sync.push_modify(rel)
            await asyncio.sleep(0.05)
