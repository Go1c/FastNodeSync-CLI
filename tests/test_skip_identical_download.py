"""Tests for skipping downloads of attachments already present locally.

Incremental file reporting leaves unchanged attachments out of the FileSync
request. The server only looks at its ``updated_timestamp > lastTime`` window,
so an attachment uploaded by this client in the previous round is answered
with FileSyncUpdate — "the client does not have it". Without a check the
client would download everything it just uploaded. FileSyncUpdate carries
``contentHash`` and ``size`` for attachments (3.0.0 and later), so the local
file can be compared first.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fns_cli.file_sync import FileSync
from fns_cli.hash_utils import file_content_hash_binary
from fns_cli.protocol import (
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_UPDATE,
    WSMessage,
)
from fns_cli.state import SyncState


def _make_engine(vault: Path, state: SyncState) -> MagicMock:
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
    engine.state = state
    engine.ws_client = ws
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    return WSMessage(action, {"data": inner})


def _write(path: Path, data: bytes, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime / 1000.0, mtime / 1000.0))


class TestSkipIdenticalDownload(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.state = SyncState(_path=str(self.vault / ".fns_state.json"))
        self.engine = _make_engine(self.vault, self.state)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _start_round(self, last_time: int = 500) -> None:
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": last_time,
            "needModifyCount": 1,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))

    def _requested_downloads(self) -> list[str]:
        return [
            call.args[0].data["path"]
            for call in self.engine.ws_client.send.await_args_list
            if call.args[0].action == "FileChunkDownload"
        ]

    async def test_identical_attachment_is_not_downloaded(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "contentHash": file_content_hash_binary(self.vault / "a.bin"),
            "size": 3,
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), [])
        self.assertEqual(self.fs.received_detail_count, 1)
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.state.last_file_sync_time, 500)
        self.assertEqual(
            self.state.file_baseline["a.bin"]["hash"],
            file_content_hash_binary(self.vault / "a.bin"),
        )

    async def test_different_hash_still_downloads(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "contentHash": file_content_hash_path(b"xyz"),
            "size": 3,
            "mtime": 2_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["a.bin"])
        self.assertIn("a.bin", self.fs._pending_download_paths)
        self.assertFalse(self.fs.is_sync_complete)

    async def test_different_size_still_downloads(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "contentHash": file_content_hash_binary(self.vault / "a.bin"),
            "size": 4,
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["a.bin"])

    async def test_missing_local_file_still_downloads(self):
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "missing.bin",
            "contentHash": "some-hash",
            "size": 3,
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["missing.bin"])

    async def test_directory_at_path_still_downloads(self):
        (self.vault / "adir").mkdir()
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "adir",
            "contentHash": "some-hash",
            "size": 0,
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["adir"])

    async def test_missing_content_hash_still_downloads(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "size": 3,
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["a.bin"])

    async def test_missing_size_still_downloads(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "contentHash": file_content_hash_binary(self.vault / "a.bin"),
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["a.bin"])

    async def test_mistyped_size_still_downloads(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        await self._start_round()

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "a.bin",
            "contentHash": file_content_hash_binary(self.vault / "a.bin"),
            "size": "3",
            "mtime": 1_000_000,
        }))

        self.assertEqual(self._requested_downloads(), ["a.bin"])

    async def test_fast_path_does_not_hash_the_file(self):
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        stat = (self.vault / "a.bin").stat()
        SyncState.set_baseline(
            self.state.file_baseline,
            "a.bin",
            file_content_hash_binary(self.vault / "a.bin"),
            int(stat.st_mtime * 1000),
            stat.st_size,
        )
        await self._start_round()

        with patch(
            "fns_cli.file_sync.file_content_hash_binary",
            wraps=file_content_hash_binary,
        ) as hashed:
            await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
                "path": "a.bin",
                "contentHash": self.state.file_baseline["a.bin"]["hash"],
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            }))

        self.assertFalse(hashed.called)
        self.assertEqual(self._requested_downloads(), [])
        self.assertTrue(self.fs.is_sync_complete)

    async def test_fast_path_miss_falls_back_to_hashing(self):
        """A touched mtime invalidates the shortcut, not the comparison."""
        _write(self.vault / "a.bin", b"abc", 1_000_000)
        content_hash = file_content_hash_binary(self.vault / "a.bin")
        SyncState.set_baseline(
            self.state.file_baseline, "a.bin", content_hash, 1, 3,
        )
        os.utime(self.vault / "a.bin", (2_000.0, 2_000.0))
        stat = (self.vault / "a.bin").stat()
        await self._start_round()

        with patch(
            "fns_cli.file_sync.file_content_hash_binary",
            wraps=file_content_hash_binary,
        ) as hashed:
            await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
                "path": "a.bin",
                "contentHash": content_hash,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            }))

        self.assertTrue(hashed.called)
        self.assertEqual(self._requested_downloads(), [])
        self.assertEqual(
            self.state.file_baseline["a.bin"]["mtime"],
            int(stat.st_mtime * 1000),
        )


def file_content_hash_path(data: bytes) -> str:
    """Hash of an imaginary remote file, without writing it to the vault."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        name = tmp.name
    try:
        return file_content_hash_binary(Path(name))
    finally:
        os.unlink(name)


if __name__ == "__main__":
    unittest.main()
