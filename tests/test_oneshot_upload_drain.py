"""Unit tests for one-shot command shutdown ordering."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fns_cli.config import AppConfig
from fns_cli.protocol import ACTION_FILE_SYNC_END, WSMessage
from fns_cli.sync_engine import SyncEngine


class _FakeWSClient:
    """WSClient stand-in: records sends and when close() is called."""

    def __init__(self, on_close):
        self.on_close = on_close
        self.sent = []
        self.closed = False

    async def run(self):
        await asyncio.Future()

    async def wait_ready(self, timeout=30):
        return True

    async def close(self):
        self.closed = True
        self.on_close()

    def on_reconnect(self, handler):
        pass

    def on(self, action, handler):
        pass

    def on_binary(self, handler):
        pass

    async def send(self, msg):
        self.sent.append(msg)

    async def send_bytes(self, data):
        pass


class TestOneShotWaitsForUploads(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        config = AppConfig()
        config.sync.watch_path = str(self.vault)
        config.sync.sync_notes = False
        config.sync.sync_files = True
        config.sync.sync_config = False

        self.close_snapshots = []
        self.engine = SyncEngine(config)
        self.engine.ws_client = _FakeWSClient(self._snapshot_close)
        self.engine.file_sync.engine = self.engine
        self.gate = asyncio.Event()
        self.chunk_started = asyncio.Event()
        self.engine.ws_client.send_bytes = self._blocked_send_bytes

        # Replace the real FileSync request with one that delivers
        # FileSyncEnd and then an upload detail for a local file.
        async def fake_request_sync():
            await self.engine.file_sync._on_sync_end(WSMessage(ACTION_FILE_SYNC_END, {
                "data": {
                    "lastTime": 111,
                    "needModifyCount": 0,
                    "needDeleteCount": 0,
                    "needUploadCount": 1,
                    "needSyncMtimeCount": 0,
                },
            }))
            await self.engine.file_sync._on_upload_session(WSMessage("FileUpload", {
                "data": {
                    "sessionId": "12345678-1234-1234-1234-123456789012",
                    "path": "big.bin",
                    "chunkSize": 2,
                },
            }))

        (self.vault / "big.bin").write_bytes(b"abcdef")
        self.engine.file_sync.request_sync = fake_request_sync

    async def asyncTearDown(self):
        self.gate.set()
        for worker in self.engine.file_sync._upload_workers:
            worker.cancel()
        await asyncio.gather(*self.engine.file_sync._upload_workers, return_exceptions=True)
        self._tmp.cleanup()

    def _snapshot_close(self):
        self.close_snapshots.append({
            "upload_tasks": len(self.engine.file_sync._upload_tasks),
            "queue_pending": self.engine.file_sync._upload_queue_pending(),
        })

    async def _blocked_send_bytes(self, _data):
        self.chunk_started.set()
        await self.gate.wait()

    async def _ack_when_awaiting(self, name: str, timeout: float = 2.0) -> None:
        """Ack the upload once its last chunk is out, as the server does."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if name in self.engine.file_sync._awaiting_ack:
                await self.engine.file_sync._on_upload_ack(
                    WSMessage("FileUploadAck", {"data": {"path": name}}),
                )
                return
            await asyncio.sleep(0.01)
        self.fail(f"upload {name} never waited for an ack")

    async def test_sync_once_waits_for_upload_before_close(self):
        task = asyncio.create_task(self.engine.sync_once())
        await asyncio.wait_for(self.chunk_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        self.assertEqual(self.close_snapshots, [], "socket closed while upload was in flight")

        self.gate.set()
        # The connection must also stay open until the upload is acked.
        await self._ack_when_awaiting("big.bin")
        await asyncio.wait_for(task, timeout=2)

        self.assertTrue(self.engine.ws_client.closed)
        self.assertEqual(len(self.close_snapshots), 1)
        self.assertEqual(self.close_snapshots[0], {"upload_tasks": 0, "queue_pending": False})
        self.assertEqual(self.engine.state.last_file_sync_time, 111)

    async def test_pull_waits_for_upload_before_close(self):
        task = asyncio.create_task(self.engine.pull())
        await asyncio.wait_for(self.chunk_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        self.assertEqual(self.close_snapshots, [], "socket closed while upload was in flight")

        self.gate.set()
        await self._ack_when_awaiting("big.bin")
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(self.close_snapshots[0], {"upload_tasks": 0, "queue_pending": False})


class TestStalledLogCounts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config = AppConfig()
        config.sync.watch_path = self._tmp.name
        self.engine = SyncEngine(config)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_stalled_log_reports_all_four_detail_counts(self):
        fs = self.engine.file_sync
        fs._expected_upload = 1
        fs._expected_mtime = 1
        fs._expected_modify = 1
        fs._expected_delete = 0
        fs._received_upload = 1
        fs.is_stalled = lambda stale_seconds: True

        with self.assertLogs("fns_cli.sync_engine", level="WARNING") as logs:
            await self.engine._wait_file_sync(timeout=1)

        self.assertTrue(any("expected=3, received=1" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
