"""Detail counting for FileSync: upload/mtime details advance the round."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fns_cli.file_sync import FileSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_PAGE,
    ACTION_FILE_SYNC_PAGE_ACK,
)

from test_file_sync import _make_engine, _wrap


class TestFileSyncAllEndCounts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        for worker in self.fs._upload_workers:
            worker.cancel()
        await asyncio.gather(*self.fs._upload_workers, return_exceptions=True)
        self._tmp.cleanup()

    async def test_upload_only_sync_does_not_wait_for_upload_completion(self):
        target = self.vault / "offline.bin"
        target.write_bytes(b"offline")
        gate = asyncio.Event()

        async def blocked_send_bytes(_data):
            await gate.wait()

        self.engine.ws_client.send_bytes.side_effect = blocked_send_bytes

        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5678,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        self.assertEqual(self.engine.ws_client.sent[0].data["pageIndex"], -1)
        self.assertFalse(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 0)

        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "offline.bin", "chunkSize": 7,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 5678)
        gate.set()
        await asyncio.gather(*self.fs._upload_tasks)

    async def test_mtime_only_sync_waits_for_mtime_detail(self):
        target = self.vault / "note.bin"
        target.write_bytes(b"same")
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 6789,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 1,
        }))
        self.assertEqual(self.engine.ws_client.sent[0].action, ACTION_FILE_SYNC_PAGE_ACK)
        self.assertEqual(self.engine.ws_client.sent[0].data["pageIndex"], -1)
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs._on_sync_mtime(_wrap("FileSyncMtime", {
            "path": "note.bin", "mtime": 1000000,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(int(target.stat().st_mtime * 1000), 1000000)
        self.assertEqual(self.engine.state.last_file_sync_time, 6789)

    async def test_upload_detail_missing_file_still_counts(self):
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5679,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "gone.bin", "chunkSize": 7,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 5679)

    async def test_upload_detail_without_session_still_counts(self):
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5680,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        await self.fs._on_upload_session(_wrap("FileUpload", {"path": "x.bin"}))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 5680)

    async def test_upload_task_registered_before_round_completes(self):
        target = self.vault / "asset.bin"
        target.write_bytes(b"abc")
        gate = asyncio.Event()

        async def blocked_send_bytes(_data):
            await gate.wait()

        self.engine.ws_client.send_bytes.side_effect = blocked_send_bytes

        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5681,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "asset.bin", "chunkSize": 1,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(len(self.fs._upload_tasks), 1)

        gate.set()
        await asyncio.wait_for(asyncio.gather(*self.fs._upload_tasks), timeout=1)

    async def test_upload_outside_vault_is_not_sent_but_counts(self):
        outside = self.vault.parent / "outside-asset.bin"
        outside.write_bytes(b"secret")
        self.addCleanup(outside.unlink)
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5682,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))

        with self.assertLogs("fns_cli.file_sync", level="WARNING"):
            await self.fs._on_upload_session(_wrap("FileUpload", {
                "sessionId": "12345678-1234-1234-1234-123456789012",
                "path": "../outside-asset.bin", "chunkSize": 7,
            }))

        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 5682)
        self.assertEqual(self.engine.ws_client.send_bytes.await_count, 0)
        self.assertEqual(self.fs._upload_tasks, set())

    async def test_upload_outside_round_does_not_repeat_complete(self):
        # Watcher-driven FileUploadCheck replies (FileUpload) arrive after the
        # sync round is over; they must not count or repeat "sync complete".
        target = self.vault / "later.bin"
        target.write_bytes(b"x")
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 5683,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 0,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        received_before = self.fs._received_upload

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        logger = logging.getLogger("fns_cli.file_sync")
        logger.addHandler(handler)
        try:
            await self.fs._on_upload_session(_wrap("FileUpload", {
                "sessionId": "12345678-1234-1234-1234-123456789012",
                "path": "later.bin", "chunkSize": 7,
            }))
        finally:
            logger.removeHandler(handler)
        self.assertFalse(any("FileSync complete" in m for m in records), records)

        self.assertEqual(self.fs._received_upload, received_before)
        self.assertEqual(self.engine.state.last_file_sync_time, 5683)
        await asyncio.wait_for(asyncio.gather(*self.fs._upload_tasks), timeout=1)

    async def test_zero_mtime_detail_still_completes_round(self):
        target = self.vault / "note.bin"
        target.write_bytes(b"same")
        before = target.stat().st_mtime
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 6790,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 1,
        }))
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs._on_sync_mtime(_wrap("FileSyncMtime", {
            "path": "note.bin", "mtime": 0,
        }))
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.engine.state.last_file_sync_time, 6790)
        self.assertEqual(target.stat().st_mtime, before)


class TestFileSyncWaitUploadsIdle(unittest.IsolatedAsyncioTestCase):
    """One-shot commands must not close the socket while uploads are running."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        for worker in self.fs._upload_workers:
            worker.cancel()
        await asyncio.gather(*self.fs._upload_workers, return_exceptions=True)
        self._tmp.cleanup()

    async def _start_blocked_upload(self, name: str = "big.bin") -> asyncio.Event:
        (self.vault / name).write_bytes(b"abcdef")
        gate = asyncio.Event()
        chunks_sent = []

        async def blocked_send_bytes(data):
            chunks_sent.append(data)
            await gate.wait()

        self.engine.ws_client.send_bytes.side_effect = blocked_send_bytes
        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": name, "chunkSize": 2,
        }))
        # Let the worker pick the session up and block on the first chunk.
        for _ in range(10):
            await asyncio.sleep(0)
            if chunks_sent:
                break
        self.assertTrue(chunks_sent, "upload worker did not start sending")
        return gate

    async def _ack_when_awaiting(self, name: str, timeout: float = 2.0) -> None:
        """Ack the upload once its last chunk is out, as the server does."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if name in self.fs._awaiting_ack:
                await self.fs._on_upload_ack(_wrap("FileUploadAck", {"path": name}))
                return
            await asyncio.sleep(0.01)
        self.fail(f"upload {name} never waited for an ack")

    async def test_waits_for_in_flight_upload(self):
        gate = await self._start_blocked_upload()
        waiter = asyncio.create_task(self.fs.wait_uploads_idle(progress_timeout=5))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done(), "must wait while an upload is in flight")

        gate.set()
        # wait_uploads_idle also waits for the ack that follows the last
        # chunk; deliver it the way the server would.
        await self._ack_when_awaiting("big.bin")
        await asyncio.wait_for(waiter, timeout=2)
        self.assertEqual(self.fs._upload_tasks, set())

    async def test_returns_immediately_when_idle(self):
        await asyncio.wait_for(self.fs.wait_uploads_idle(progress_timeout=5), timeout=0.5)

    async def test_gives_up_after_no_progress(self):
        gate = await self._start_blocked_upload()
        with self.assertLogs("fns_cli.file_sync", level="WARNING") as logs:
            await asyncio.wait_for(self.fs.wait_uploads_idle(progress_timeout=0.2), timeout=2)
        self.assertTrue(any("without progress" in line for line in logs.output))
        gate.set()
        await asyncio.gather(*self.fs._upload_tasks, return_exceptions=True)

    async def test_progress_refreshes_timeout(self):
        (self.vault / "multi.bin").write_bytes(b"abcdefgh")
        chunk_times = []

        async def paced_send_bytes(_data):
            chunk_times.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.05)

        self.engine.ws_client.send_bytes.side_effect = paced_send_bytes
        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "multi.bin", "chunkSize": 2,
        }))
        # 4 chunks * 0.05s = 0.2s total, longer than the 0.15s progress
        # timeout, but no single gap exceeds it. The ack keeps the second
        # (ack) wait from running into its own timeout.
        waiter = asyncio.create_task(
            self.fs.wait_uploads_idle(progress_timeout=0.15),
        )
        await self._ack_when_awaiting("multi.bin")
        await asyncio.wait_for(waiter, timeout=2)
        self.assertEqual(len(chunk_times), 4)
        self.assertEqual(self.fs._upload_tasks, set())


class TestFileSyncActivityRefresh(unittest.IsolatedAsyncioTestCase):
    """Upload details and pages must refresh the stall timer."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        for worker in self.fs._upload_workers:
            worker.cancel()
        await asyncio.gather(*self.fs._upload_workers, return_exceptions=True)
        self._tmp.cleanup()

    async def _stalled_after_end(self):
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 999,
            "needModifyCount": 5,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 0,
        }))
        with patch(
            "fns_cli.file_sync.time.monotonic",
            return_value=self.fs._last_sync_activity_monotonic + 6,
        ):
            self.assertTrue(self.fs.is_stalled(5))

    async def test_upload_detail_refreshes_activity(self):
        await self._stalled_after_end()

        await self.fs._on_upload_session(_wrap("FileUpload", {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "gone.bin", "chunkSize": 7,
        }))
        with patch(
            "fns_cli.file_sync.time.monotonic",
            return_value=self.fs._last_sync_activity_monotonic + 1,
        ):
            self.assertFalse(self.fs.is_stalled(5))

    async def test_sync_page_refreshes_activity(self):
        await self._stalled_after_end()

        await self.fs._on_sync_page(_wrap(ACTION_FILE_SYNC_PAGE, {
            "pageIndex": 0, "isLast": False, "items": [],
        }))
        with patch(
            "fns_cli.file_sync.time.monotonic",
            return_value=self.fs._last_sync_activity_monotonic + 1,
        ):
            self.assertFalse(self.fs.is_stalled(5))

if __name__ == "__main__":
    unittest.main()
