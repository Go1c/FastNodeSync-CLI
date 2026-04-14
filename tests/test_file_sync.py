"""Unit tests for FileSync counter logic and delete behaviour.

These tests use a fake SyncEngine / WSClient so no real server is needed.
Run with:  python -m pytest tests/test_file_sync.py -v
       or: python -m unittest tests/test_file_sync.py
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.file_sync import FileSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_DELETE,
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_UPDATE,
    WSMessage,
)


def _make_engine(vault_path: Path) -> MagicMock:
    """Build a minimal fake SyncEngine."""
    config = MagicMock()
    config.server.vault = "test-vault"
    config.sync.file_chunk_size = 1024 * 1024

    state = MagicMock()
    state.last_file_sync_time = 0

    ws = MagicMock()
    ws.send = AsyncMock()

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault_path
    engine.state = state
    engine.ws_client = ws
    engine.ignore_file = MagicMock()
    engine.unignore_file = MagicMock()
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    """Wrap a payload the same way the server does: {data: {...}}."""
    return WSMessage(action, {"data": inner})


class TestFileSyncDeleteCounter(unittest.IsolatedAsyncioTestCase):
    """FileSyncEnd arrives BEFORE FileSyncDelete (server's normal order)."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────

    async def _send_end(self, *, need_modify=0, need_delete=0, last_time=1000):
        msg = _wrap(ACTION_FILE_SYNC_END, {
            "lastTime": last_time,
            "needModifyCount": need_modify,
            "needDeleteCount": need_delete,
            "needUploadCount": 0,
        })
        await self.fs._on_sync_end(msg)

    async def _send_delete(self, rel_path: str):
        msg = _wrap(ACTION_FILE_SYNC_DELETE, {"path": rel_path})
        await self.fs._on_sync_delete(msg)

    async def _send_update_text(self, rel_path: str, content: str):
        msg = _wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": rel_path,
            "content": content,
            "mtime": 1000000,
        })
        await self.fs._on_sync_update(msg)

    # ── tests ─────────────────────────────────────────────────────────

    async def test_nothing_to_do_completes_immediately(self):
        await self._send_end(need_modify=0, need_delete=0)
        self.assertTrue(self.fs.is_sync_complete)

    async def test_end_before_delete_stays_incomplete(self):
        """Sync must NOT complete until the delete message arrives."""
        target = self.vault / "photo.png"
        target.write_bytes(b"\x89PNG")

        await self._send_end(need_delete=1)
        self.assertFalse(self.fs.is_sync_complete, "should wait for delete")

        await self._send_delete("photo.png")
        self.assertTrue(self.fs.is_sync_complete)
        self.assertFalse(target.exists(), "file must be deleted from disk")

    async def test_delete_before_end_completes_on_end(self):
        """If delete arrives first, completing on FileSyncEnd is also correct."""
        target = self.vault / "old.pdf"
        target.write_bytes(b"%PDF")

        await self._send_delete("old.pdf")
        self.assertFalse(self.fs.is_sync_complete)

        await self._send_end(need_delete=1)
        self.assertTrue(self.fs.is_sync_complete)
        self.assertFalse(target.exists())

    async def test_multiple_deletes(self):
        files = ["a.png", "b.jpg", "c.pdf"]
        for f in files:
            (self.vault / f).write_bytes(b"data")

        await self._send_end(need_delete=3)
        self.assertFalse(self.fs.is_sync_complete)

        for f in files[:-1]:
            await self._send_delete(f)
            self.assertFalse(self.fs.is_sync_complete)

        await self._send_delete(files[-1])
        self.assertTrue(self.fs.is_sync_complete)
        for f in files:
            self.assertFalse((self.vault / f).exists())

    async def test_delete_nonexistent_file_still_counts(self):
        """Server may send delete for a file we don't have — still must count."""
        await self._send_end(need_delete=1)
        await self._send_delete("ghost.png")
        self.assertTrue(self.fs.is_sync_complete)

    async def test_text_update_and_delete(self):
        target = self.vault / "note.md"
        to_delete = self.vault / "old.png"
        to_delete.write_bytes(b"old")

        await self._send_end(need_modify=1, need_delete=1)
        self.assertFalse(self.fs.is_sync_complete)

        await self._send_update_text("note.md", "hello world")
        self.assertFalse(self.fs.is_sync_complete)

        await self._send_delete("old.png")
        self.assertTrue(self.fs.is_sync_complete)

        self.assertEqual(target.read_text(), "hello world")
        self.assertFalse(to_delete.exists())

    async def test_reset_counters_on_new_request_sync(self):
        """request_sync must reset state so a second sync works cleanly."""
        await self._send_end(need_delete=1)
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs.request_sync()          # triggers _reset_counters
        self.assertFalse(self.fs._got_end)
        self.assertEqual(self.fs._received_delete, 0)

    async def test_unexpected_content_type_does_not_stall(self):
        """Unknown content type must still count as received (no 300s stall)."""
        msg = _wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "weird.bin",
            "content": 12345,           # int — unexpected type
            "mtime": 0,
        })
        await self._send_end(need_modify=1)
        await self.fs._on_sync_update(msg)
        self.assertTrue(self.fs.is_sync_complete)


class TestFileSyncWatcherIgnore(unittest.TestCase):
    """Watcher on_moved must honour is_ignored to avoid echo-backs."""

    def _make_handler(self, vault: Path, ignored: set[str]):
        from fns_cli.watcher import _VaultEventHandler

        engine = _make_engine(vault)
        engine.is_ignored = lambda rel: rel in ignored
        loop = asyncio.new_event_loop()
        handler = _VaultEventHandler(engine, loop)
        return handler, engine, loop

    def test_move_ignored_src_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "old.png").write_bytes(b"")
            (vault / "new.png").write_bytes(b"")

            handler, engine, loop = self._make_handler(vault, {"old.png"})

            from watchdog.events import FileMovedEvent
            ev = FileMovedEvent(str(vault / "old.png"), str(vault / "new.png"))
            handler.on_moved(ev)

            self.assertNotIn("mv:old.png:new.png", handler._pending)
            loop.close()

    def test_move_ignored_dest_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            handler, engine, loop = self._make_handler(vault, {"new.png"})

            from watchdog.events import FileMovedEvent
            ev = FileMovedEvent(str(vault / "old.png"), str(vault / "new.png"))
            handler.on_moved(ev)

            self.assertNotIn("mv:old.png:new.png", handler._pending)
            loop.close()

    def test_move_not_ignored_is_scheduled(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            handler, engine, loop = self._make_handler(vault, set())

            from watchdog.events import FileMovedEvent
            ev = FileMovedEvent(str(vault / "old.png"), str(vault / "new.png"))
            handler.on_moved(ev)

            self.assertIn("mv:old.png:new.png", handler._pending)
            # cancel to avoid loop cleanup issues
            handler._pending["mv:old.png:new.png"].cancel()
            loop.close()


if __name__ == "__main__":
    unittest.main()
