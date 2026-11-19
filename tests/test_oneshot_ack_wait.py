"""Tests for waiting on upload acknowledgements in one-shot commands.

The incremental baseline is recorded when FileUploadAck / SettingModifyAck
arrives, not when the chunks leave the client. One-shot ``sync_once`` and
``pull`` close the connection right after the sync round, so they wait for
those acks first — with a progress timeout, because not every server answers:

* the last chunk being sent is not the end of an upload; the server still
  has to write it and reply,
* 3.0.x-3.1.x answer an unchanged SettingModify with a bare success that
  names no action and no path, so no ack will ever match.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.config import AppConfig
from fns_cli.file_sync import FileSync
from fns_cli.protocol import ACTION_FILE_UPLOAD, WSMessage
from fns_cli.setting_sync import SettingSync
from fns_cli.state import SyncState
from fns_cli.sync_engine import SyncEngine


def _make_engine(vault: Path, state: SyncState | None = None) -> MagicMock:
    config = MagicMock()
    config.server.vault = "test-vault"
    config.sync.file_chunk_size = 1024 * 1024
    config.sync.upload_concurrency = 1
    config.sync.config_sync_dirs = [".obsidian"]
    config.sync.sync_config = True
    config.sync.sync_files = True

    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_bytes = AsyncMock()

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault
    engine.state = state or SyncState(_path=str(vault / ".fns_state.json"))
    engine.ws_client = ws
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    return WSMessage(action, {"data": inner})


def _schedule(coro, delay: float) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: asyncio.ensure_future(coro))


class TestFileUploadAckWait(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_no_uploads_returns_immediately(self):
        started = time.monotonic()
        await asyncio.wait_for(
            self.fs.wait_uploads_idle(ack_timeout=5.0, poll_interval=0.01),
            timeout=1,
        )
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_waits_until_the_last_ack_arrives(self):
        self.fs._awaiting_ack.update({"a.bin", "b.bin"})
        _schedule(self.fs._on_upload_ack(_wrap("FileUploadAck", {"path": "a.bin"})), 0.02)
        _schedule(self.fs._on_upload_ack(_wrap("FileUploadAck", {"path": "b.bin"})), 0.06)

        await asyncio.wait_for(
            self.fs.wait_uploads_idle(ack_timeout=2.0, poll_interval=0.01),
            timeout=2,
        )

        self.assertEqual(self.fs._awaiting_ack, set())

    async def test_missing_ack_times_out_with_a_warning(self):
        self.fs._awaiting_ack.add("a.bin")

        with self.assertLogs("fns_cli.file_sync", level="WARNING") as logs:
            await asyncio.wait_for(
                self.fs.wait_uploads_idle(ack_timeout=0.05, poll_interval=0.01),
                timeout=2,
            )

        self.assertTrue(any("upload acks" in line for line in logs.output), logs.output)
        self.assertEqual(self.fs._awaiting_ack, {"a.bin"})

    async def test_directives_that_cannot_be_uploaded_are_not_awaited(self):
        # A missing file and an unsafe path both count towards the round, but
        # no upload starts, so there is nothing to acknowledge.
        await self.fs._on_upload_session(_wrap(ACTION_FILE_UPLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1024,
            "path": "missing.bin",
        }))
        await self.fs._on_upload_session(_wrap(ACTION_FILE_UPLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1024,
            "path": "../escape.bin",
        }))

        self.assertEqual(self.fs._awaiting_ack, set())
        started = time.monotonic()
        await asyncio.wait_for(
            self.fs.wait_uploads_idle(ack_timeout=5.0, poll_interval=0.01),
            timeout=1,
        )
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_worker_registers_and_ack_clears_the_path(self):
        full = self.vault / "a.bin"
        full.write_bytes(b"abc")

        await self.fs._upload_session_worker(
            "12345678-1234-1234-1234-123456789012", 1024, "a.bin", full,
        )
        self.assertIn("a.bin", self.fs._awaiting_ack)

        await self.fs._on_upload_ack(_wrap("FileUploadAck", {"path": "a.bin"}))
        self.assertNotIn("a.bin", self.fs._awaiting_ack)


class TestSettingAckWait(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.sync = SettingSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_noop_without_pending_uploads(self):
        started = time.monotonic()
        await asyncio.wait_for(
            self.sync.wait_modify_acks(ack_timeout=5.0, poll_interval=0.01),
            timeout=1,
        )
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_waits_for_pending_ack(self):
        self.sync._pending_uploads[".obsidian/app.json"] = {
            "hash": "h", "mtime": 1, "size": 2,
        }
        _schedule(self.sync._on_modify_ack(_wrap(
            "SettingModifyAck", {"path": ".obsidian/app.json"},
        )), 0.05)

        await asyncio.wait_for(
            self.sync.wait_modify_acks(ack_timeout=2.0, poll_interval=0.01),
            timeout=2,
        )

        self.assertEqual(self.sync._pending_uploads, {})

    async def test_missing_ack_times_out_quietly(self):
        """Old servers never send a matching ack; that must not be an error."""
        self.sync._pending_uploads[".obsidian/app.json"] = {
            "hash": "h", "mtime": 1, "size": 2,
        }

        await asyncio.wait_for(
            self.sync.wait_modify_acks(ack_timeout=0.05, poll_interval=0.01),
            timeout=2,
        )

        self.assertIn(".obsidian/app.json", self.sync._pending_uploads)


class _FakeWSClient:
    """WSClient stand-in for SyncEngine.sync_once / pull."""

    def __init__(self):
        self.closed = False
        self.sent = []

    async def run(self):
        await asyncio.Future()

    async def wait_ready(self, timeout=30):
        return True

    async def close(self):
        self.closed = True

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


class TestOneShotWaitsInOrder(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.sync.watch_path = self._tmp.name
        self.config.sync.sync_notes = False
        self.config.sync.sync_files = False
        self.config.sync.sync_config = False
        self.engine = SyncEngine(self.config)
        self.events: list[str] = []

        async def fake_close():
            self.events.append("close")
            self.engine.ws_client.closed = True

        async def fake_file_wait(*args, **kwargs):
            self.events.append("file-acks")

        async def fake_setting_wait(*args, **kwargs):
            self.events.append("setting-acks")

        self.engine.ws_client = _FakeWSClient()
        self.engine.ws_client.close = fake_close
        self.engine.file_sync.wait_uploads_idle = fake_file_wait
        self.engine.setting_sync.wait_modify_acks = fake_setting_wait

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_sync_once_waits_for_both_then_closes(self):
        await asyncio.wait_for(self.engine.sync_once(), timeout=2)
        self.assertEqual(self.events, ["file-acks", "setting-acks", "close"])

    async def test_pull_waits_for_both_then_closes(self):
        await asyncio.wait_for(self.engine.pull(), timeout=2)
        self.assertEqual(self.events, ["file-acks", "setting-acks", "close"])


if __name__ == "__main__":
    unittest.main()
