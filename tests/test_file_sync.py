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
from unittest.mock import patch

from fns_cli.file_sync import FileSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_CHUNK_DOWNLOAD,
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
    config.sync.upload_concurrency = 1

    state = MagicMock()
    state.last_file_sync_time = 0

    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_bytes = AsyncMock()

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

    async def test_last_time_commits_only_after_chunk_download_finishes(self):
        session_id = "12345678-1234-1234-1234-123456789012"

        await self._send_end(need_modify=1, last_time=4321)
        self.assertEqual(self.engine.state.last_file_sync_time, 0)

        msg = _wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "asset.bin",
            "content": None,
            "size": 3,
            "totalChunks": 1,
            "chunkSize": 3,
        })
        await self.fs._on_sync_update(msg)
        self.assertEqual(self.engine.state.last_file_sync_time, 0)
        self.assertIn("asset.bin", self.fs._pending_download_paths)

        start = _wrap(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, {
            "sessionId": session_id,
            "path": "asset.bin",
            "size": 3,
            "totalChunks": 1,
            "chunkSize": 3,
        })
        await self.fs._on_chunk_download_start(start)
        await self.fs._on_binary_chunk(session_id, 0, b"abc")

        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 4321)
        self.engine.state.save.assert_called()
        self.assertEqual((self.vault / "asset.bin").read_bytes(), b"abc")

    async def test_zero_chunk_download_completes_immediately(self):
        msg = _wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "empty.bin",
            "content": None,
            "size": 0,
            "totalChunks": 0,
            "chunkSize": 1024,
        })
        start = _wrap(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "empty.bin",
            "size": 0,
            "totalChunks": 0,
            "chunkSize": 1024,
        })

        await self._send_end(need_modify=1, last_time=5555)
        await self.fs._on_sync_update(msg)
        await self.fs._on_chunk_download_start(start)

        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 5555)
        self.assertEqual((self.vault / "empty.bin").read_bytes(), b"")

    async def test_request_sync_clears_stale_download_state(self):
        self.fs._download_sessions["stale"] = object()
        self.fs._pending_download_paths.add("stale.bin")

        await self.fs.request_sync()

        self.assertEqual(self.fs._download_sessions, {})
        self.assertEqual(self.fs._pending_download_paths, set())

    async def test_upload_session_is_scheduled_without_blocking_handler(self):
        target = self.vault / "asset.bin"
        target.write_bytes(b"abc")

        gate = asyncio.Event()

        async def slow_send_bytes(_data):
            await gate.wait()

        self.engine.ws_client.send_bytes.side_effect = slow_send_bytes

        msg = _wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1,
            "path": "asset.bin",
        })
        await self.fs._on_upload_session(msg)

        self.assertEqual(len(self.fs._upload_tasks), 1)
        gate.set()
        await asyncio.wait_for(asyncio.gather(*self.fs._upload_tasks), timeout=1)

    async def test_is_stalled_when_end_counts_never_arrive(self):
        await self._send_end(need_delete=1)

        with patch("fns_cli.file_sync.time.monotonic", return_value=self.fs._last_sync_activity_monotonic + 6):
            self.assertTrue(self.fs.is_stalled(5))

    async def test_is_not_stalled_while_download_pending(self):
        await self._send_end(need_modify=1)
        self.fs._pending_download_paths.add("asset.bin")

        with patch("fns_cli.file_sync.time.monotonic", return_value=self.fs._last_sync_activity_monotonic + 6):
            self.assertFalse(self.fs.is_stalled(5))

    async def test_upload_sessions_respect_concurrency_limit(self):
        first = self.vault / "first.bin"
        second = self.vault / "second.bin"
        first.write_bytes(b"a")
        second.write_bytes(b"b")

        started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def blocking_send_bytes(_data):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            started.set()
            await release.wait()
            active -= 1

        self.engine.ws_client.send_bytes.side_effect = blocking_send_bytes

        msg1 = _wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1,
            "path": "first.bin",
        })
        msg2 = _wrap("FileUpload", {
            "sessionId": "22345678-1234-1234-1234-123456789012",
            "chunkSize": 1,
            "path": "second.bin",
        })

        await self.fs._on_upload_session(msg1)
        await self.fs._on_upload_session(msg2)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)

        self.assertEqual(max_active, 1)
        self.assertEqual(self.engine.ws_client.send_bytes.await_count, 1)

        release.set()
        await asyncio.wait_for(asyncio.gather(*self.fs._upload_tasks), timeout=1)


class TestFileSyncWatcherIgnore(unittest.TestCase):
    """Watcher on_moved must honour is_ignored to avoid echo-backs."""

    def _make_handler(
        self,
        vault: Path,
        ignored: set[str],
        excluded: set[str] | None = None,
    ):
        from fns_cli.watcher import _VaultEventHandler

        engine = _make_engine(vault)
        engine.is_ignored = lambda rel: rel in ignored
        excluded = excluded or set()
        engine.is_excluded = lambda rel: rel in excluded
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

    def test_move_into_excluded_schedules_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            handler, engine, loop = self._make_handler(
                vault,
                set(),
                {"trash/new.png"},
            )

            from watchdog.events import FileMovedEvent
            ev = FileMovedEvent(
                str(vault / "old.png"),
                str(vault / "trash" / "new.png"),
            )
            handler.on_moved(ev)

            self.assertIn("del:old.png", handler._pending)
            handler._pending["del:old.png"].cancel()
            loop.close()

    def test_move_from_outside_into_vault_schedules_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            outside = Path(tempfile.mkdtemp())
            try:
                handler, engine, loop = self._make_handler(vault, set())

                from watchdog.events import FileMovedEvent
                ev = FileMovedEvent(
                    str(outside / "incoming.png"),
                    str(vault / "incoming.png"),
                )
                handler.on_moved(ev)

                self.assertIn("mod:incoming.png", handler._pending)
                handler._pending["mod:incoming.png"].cancel()
                loop.close()
            finally:
                for child in outside.iterdir():
                    child.unlink()
                outside.rmdir()

    def test_move_from_vault_to_outside_schedules_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            outside = Path(tempfile.mkdtemp())
            try:
                handler, engine, loop = self._make_handler(vault, set())

                from watchdog.events import FileMovedEvent
                ev = FileMovedEvent(
                    str(vault / "outgoing.png"),
                    str(outside / "outgoing.png"),
                )
                handler.on_moved(ev)

                self.assertIn("del:outgoing.png", handler._pending)
                handler._pending["del:outgoing.png"].cancel()
                loop.close()
            finally:
                for child in outside.iterdir():
                    child.unlink()
                outside.rmdir()

    def test_directory_move_into_excluded_schedules_child_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "trash" / "renamed").mkdir(parents=True)
            (vault / "trash" / "renamed" / "a.txt").write_text("a", encoding="utf-8")
            (vault / "trash" / "renamed" / "b.txt").write_text("b", encoding="utf-8")

            handler, engine, loop = self._make_handler(
                vault,
                set(),
                {"trash/renamed/a.txt", "trash/renamed/b.txt"},
            )

            from watchdog.events import DirMovedEvent
            ev = DirMovedEvent(
                str(vault / "old"),
                str(vault / "trash" / "renamed"),
            )
            handler.on_moved(ev)

            self.assertIn("del:old/a.txt", handler._pending)
            self.assertIn("del:old/b.txt", handler._pending)
            handler._pending["del:old/a.txt"].cancel()
            handler._pending["del:old/b.txt"].cancel()
            loop.close()

    def test_directory_delete_schedules_child_file_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            doomed = vault / "12345"
            doomed.mkdir(parents=True)
            (doomed / "a.md").write_text("a", encoding="utf-8")
            (doomed / "b.png").write_bytes(b"png")
            (doomed / "nested").mkdir()
            (doomed / "nested" / "c.txt").write_text("c", encoding="utf-8")

            handler, engine, loop = self._make_handler(vault, set())

            from watchdog.events import DirDeletedEvent
            ev = DirDeletedEvent(str(vault / "12345"))
            handler.on_deleted(ev)

            self.assertIn("del:12345/a.md", handler._pending)
            self.assertIn("del:12345/b.png", handler._pending)
            self.assertIn("del:12345/nested/c.txt", handler._pending)
            handler._pending["del:12345/a.md"].cancel()
            handler._pending["del:12345/b.png"].cancel()
            handler._pending["del:12345/nested/c.txt"].cancel()
            loop.close()


if __name__ == "__main__":
    unittest.main()
