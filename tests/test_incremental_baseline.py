"""Tests for the incremental report baseline.

Incremental FileSync/SettingSync used to report every local file on every
reconnect. The server only compares entries inside its ``updated_timestamp >
lastTime`` window and treats everything else as "the client has it, the server
may not", so it asked for a re-upload of every unchanged attachment each time.

The client now keeps a per-path baseline of the state the server confirmed and
reports only paths that differ from it. These tests pin down:

  * incremental mode reports only new/changed paths,
  * the first run (no baseline) still reports everything and establishes a
    baseline afterwards,
  * a round that was not completed does not write the baseline,
  * paths the server asked to upload but never acked stay out of the baseline,
  * the state file stays compatible in both directions.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.file_sync import FileSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_UPDATE,
    ACTION_SETTING_SYNC_END,
    WSMessage,
)
from fns_cli.setting_sync import SettingSync
from fns_cli.state import SyncState


def _make_state(vault: Path) -> SyncState:
    return SyncState(_path=str(vault / ".fns_state.json"))


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


class TestStateCompatibility(unittest.TestCase):

    def test_legacy_state_file_loads_without_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / ".fns_state.json").write_text(json.dumps({
                "last_note_sync_time": 11,
                "last_file_sync_time": 22,
                "last_setting_sync_time": 33,
                "synced_note_paths": ["a.md"],
            }), encoding="utf-8")

            state = SyncState.load(vault)

        self.assertEqual(state.last_file_sync_time, 22)
        self.assertEqual(state.file_baseline, {})
        self.assertEqual(state.setting_baseline, {})

    def test_baseline_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            state = SyncState.load(vault)
            SyncState.set_baseline(state.file_baseline, "img/a.png", "123", 1000, 7)
            SyncState.set_baseline(state.setting_baseline, ".obsidian/app.json", "456", 2000, 9)
            state.save()

            reloaded = SyncState.load(vault)

        self.assertEqual(
            reloaded.file_baseline["img/a.png"],
            {"hash": "123", "mtime": 1000, "size": 7},
        )
        self.assertEqual(
            reloaded.setting_baseline[".obsidian/app.json"],
            {"hash": "456", "mtime": 2000, "size": 9},
        )

    def test_new_state_file_is_readable_by_the_old_loader(self):
        """An older client reads the same keys and ignores the new ones."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            state = SyncState.load(vault)
            state.last_file_sync_time = 42
            SyncState.set_baseline(state.file_baseline, "a.bin", "1", 2, 3)
            state.save()

            raw = json.loads((vault / ".fns_state.json").read_text(encoding="utf-8"))
            # What the previous release's SyncState.load() reads:
            self.assertEqual(raw["last_file_sync_time"], 42)
            self.assertEqual(raw.get("last_note_sync_time", 0), 0)
            self.assertEqual(raw.get("synced_note_paths", []), [])

    def test_unknown_keys_do_not_break_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / ".fns_state.json").write_text(json.dumps({
                "last_file_sync_time": 1,
                "file_baseline": {"ok.bin": {"hash": "1", "mtime": 2, "size": 3},
                                  "bad.bin": "not-a-dict"},
                "future_key": {"x": 1},
            }), encoding="utf-8")

            state = SyncState.load(vault)

        self.assertEqual(list(state.file_baseline), ["ok.bin"])


class TestFileIncrementalReport(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.state = _make_state(self.vault)
        self.engine = _make_engine(self.vault, self.state)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _seed_baseline(self):
        _write(self.vault / "unchanged.bin", b"same", 1_000_000)
        _write(self.vault / "changed.bin", b"old", 1_000_000)
        self.state.last_file_sync_time = 500
        from fns_cli.hash_utils import file_content_hash_binary
        SyncState.set_baseline(
            self.state.file_baseline,
            "unchanged.bin",
            file_content_hash_binary(self.vault / "unchanged.bin"),
            1_000_000,
            4,
        )
        SyncState.set_baseline(
            self.state.file_baseline,
            "changed.bin",
            file_content_hash_binary(self.vault / "changed.bin"),
            1_000_000,
            3,
        )

    async def test_incremental_reports_only_new_and_changed(self):
        await self._seed_baseline()
        _write(self.vault / "changed.bin", b"new-content", 2_000_000)
        _write(self.vault / "new.bin", b"fresh", 2_000_000)

        await self.fs.request_sync()

        msg = self.engine.ws_client.send.await_args.args[0]
        reported = {entry["path"] for entry in msg.data["files"]}
        self.assertEqual(reported, {"changed.bin", "new.bin"})

    async def test_touched_mtime_with_same_content_is_not_reported(self):
        """A backup/extract tool refreshing mtime must not cause a re-upload."""
        await self._seed_baseline()
        os.utime(self.vault / "unchanged.bin", (9_000.0, 9_000.0))

        await self.fs.request_sync()

        msg = self.engine.ws_client.send.await_args.args[0]
        self.assertEqual(msg.data["files"], [])

    async def test_first_run_reports_everything_then_establishes_baseline(self):
        _write(self.vault / "a.bin", b"aaa", 1_000_000)
        _write(self.vault / "b.bin", b"bbb", 1_000_000)
        self.state.last_file_sync_time = 0

        await self.fs.request_sync()
        msg = self.engine.ws_client.send.await_args.args[0]
        self.assertEqual(len(msg.data["files"]), 2)

        # Server has no changes and asks for no uploads.
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 777,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))

        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(set(self.state.file_baseline), {"a.bin", "b.bin"})
        self.assertEqual(self.state.last_file_sync_time, 777)

    async def test_second_round_reports_nothing_when_nothing_changed(self):
        """End to end: the point of the change — no re-report on reconnect."""
        _write(self.vault / "a.bin", b"aaa", 1_000_000)
        _write(self.vault / "b.bin", b"bbb", 1_000_000)

        await self.fs.request_sync()
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 777,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))

        await self.fs.request_sync()
        msg = self.engine.ws_client.send.await_args.args[0]
        self.assertEqual(msg.data["lastTime"], 777)
        self.assertEqual(msg.data["files"], [])

    async def test_interrupted_round_does_not_commit_baseline(self):
        _write(self.vault / "a.bin", b"aaa", 1_000_000)
        self.state.last_file_sync_time = 0

        await self.fs.request_sync()
        # The server says one file has to be modified but never sends it.
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 777,
            "needModifyCount": 1,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))

        self.assertFalse(self.fs.is_sync_complete)
        self.assertEqual(self.state.file_baseline, {})
        self.assertEqual(self.state.last_file_sync_time, 0)

    async def test_undelivered_upload_directives_keep_paths_uncommitted(self):
        """needUploadCount without a usable upload directive.

        A directive that cannot be acted on (here: an unsafe path) still
        counts towards round completion, as the server counts the frames it
        sends. When fewer usable directives arrived than the server announced,
        it is impossible to tell which reported paths it wanted, so nothing
        from the round is committed and a.bin is reported again next time.
        """
        _write(self.vault / "a.bin", b"aaa", 1_000_000)
        self.state.last_file_sync_time = 0

        await self.fs.request_sync()
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 777,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
        }))
        from fns_cli.protocol import ACTION_FILE_UPLOAD
        await self.fs._on_upload_session(_wrap(ACTION_FILE_UPLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1024,
            "path": "../escape.bin",
        }))

        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(self.state.file_baseline, {})
        self.assertNotIn("a.bin", self.state.file_baseline)

        # The file is still reported on the next round.
        await self.fs.request_sync()
        msg = self.engine.ws_client.send.await_args.args[0]
        self.assertEqual([entry["path"] for entry in msg.data["files"]], ["a.bin"])

    async def test_upload_ack_commits_only_the_acked_path(self):
        _write(self.vault / "a.bin", b"aaa", 1_000_000)
        _write(self.vault / "b.bin", b"bbb", 1_000_000)
        self.state.last_file_sync_time = 0

        await self.fs.request_sync()
        await self.fs._on_sync_end(_wrap(ACTION_FILE_SYNC_END, {
            "lastTime": 777,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
        }))
        # The server asked for a.bin only. The round completes when the
        # FileUpload directive arrives, which confirms b.bin (reported, not
        # asked for) but must leave a.bin pending until its ack.
        from fns_cli.protocol import ACTION_FILE_UPLOAD
        await self.fs._on_upload_session(_wrap(ACTION_FILE_UPLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "chunkSize": 1024,
            "path": "a.bin",
        }))

        self.assertTrue(self.fs.is_sync_complete)
        self.assertIn("b.bin", self.state.file_baseline)
        self.assertNotIn("a.bin", self.state.file_baseline)

        await self.fs._on_upload_ack(_wrap("FileUploadAck", {"path": "a.bin"}))

        self.assertIn("a.bin", self.state.file_baseline)

    async def test_downloaded_file_is_in_baseline(self):
        self.state.last_file_sync_time = 0
        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": "remote.bin",
            "content": None,
            "size": 3,
            "totalChunks": 1,
            "chunkSize": 3,
        }))
        from fns_cli.protocol import ACTION_FILE_SYNC_CHUNK_DOWNLOAD
        await self.fs._on_chunk_download_start(_wrap(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": "remote.bin",
            "size": 3,
            "totalChunks": 1,
            "chunkSize": 3,
        }))
        await self.fs._on_binary_chunk(
            "12345678-1234-1234-1234-123456789012", 0, b"abc",
        )

        self.assertIn("remote.bin", self.state.file_baseline)
        self.assertEqual(self.state.file_baseline["remote.bin"]["size"], 3)

    async def test_deleted_path_leaves_baseline(self):
        _write(self.vault / "gone.bin", b"x", 1_000_000)
        SyncState.set_baseline(self.state.file_baseline, "gone.bin", "h", 1_000_000, 1)
        from fns_cli.protocol import ACTION_FILE_SYNC_DELETE
        await self.fs._on_sync_delete(_wrap(ACTION_FILE_SYNC_DELETE, {
            "path": "gone.bin",
        }))

        self.assertNotIn("gone.bin", self.state.file_baseline)
        self.assertFalse((self.vault / "gone.bin").exists())


class TestSettingIncrementalReport(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.state = _make_state(self.vault)
        self.engine = _make_engine(self.vault, self.state)
        self.sync = SettingSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_incremental_reports_only_changed_config(self):
        from fns_cli.hash_utils import file_content_hash_binary
        _write(self.vault / ".obsidian" / "app.json", b"{}", 1_000_000)
        _write(self.vault / ".obsidian" / "theme.json", b"dark", 1_000_000)
        self.state.last_setting_sync_time = 500
        SyncState.set_baseline(
            self.state.setting_baseline,
            ".obsidian/app.json",
            file_content_hash_binary(self.vault / ".obsidian" / "app.json"),
            1_000_000,
            2,
        )
        _write(self.vault / ".obsidian" / "theme.json", b"light", 2_000_000)

        await self.sync.request_sync()

        msg = self.engine.ws_client.send.await_args.args[0]
        reported = {entry["path"] for entry in msg.data["settings"]}
        self.assertEqual(reported, {".obsidian/theme.json"})

    async def test_full_sync_still_reports_everything(self):
        _write(self.vault / ".obsidian" / "app.json", b"{}", 1_000_000)
        self.state.last_setting_sync_time = 500
        SyncState.set_baseline(self.state.setting_baseline, ".obsidian/app.json", "h", 1_000_000, 2)

        await self.sync.request_full_sync()

        msg = self.engine.ws_client.send.await_args.args[0]
        self.assertEqual(msg.data["lastTime"], 0)
        self.assertEqual(len(msg.data["settings"]), 1)

    async def test_first_round_establishes_baseline(self):
        _write(self.vault / ".obsidian" / "app.json", b"{}", 1_000_000)

        await self.sync.request_sync()
        await self.sync._on_sync_end(_wrap(ACTION_SETTING_SYNC_END, {
            "lastTime": 900,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))

        self.assertTrue(self.sync.is_sync_complete)
        self.assertIn(".obsidian/app.json", self.state.setting_baseline)
        self.assertEqual(self.state.last_setting_sync_time, 900)

    async def test_need_upload_directive_is_applied_and_acked(self):
        """The server sends SettingSyncNeedUpload per path, as {path}."""
        from fns_cli.protocol import ACTION_SETTING_MODIFY_ACK, ACTION_SETTING_SYNC_NEED_UPLOAD
        _write(self.vault / ".obsidian" / "app.json", b"{}", 1_000_000)
        self.state.last_setting_sync_time = 500
        SyncState.set_baseline(
            self.state.setting_baseline,
            ".obsidian/app.json",
            "stale-hash",
            1_000_000,
            2,
        )

        await self.sync._on_sync_end(_wrap(ACTION_SETTING_SYNC_END, {
            "lastTime": 900,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
        }))
        await self.sync._on_sync_need_upload(_wrap(
            ACTION_SETTING_SYNC_NEED_UPLOAD, {"path": ".obsidian/app.json"},
        ))

        sent = [call.args[0].action for call in self.engine.ws_client.send.await_args_list]
        self.assertIn("SettingModify", sent)

        await self.sync._on_modify_ack(_wrap(
            ACTION_SETTING_MODIFY_ACK, {"path": ".obsidian/app.json"},
        ))
        from fns_cli.hash_utils import file_content_hash_binary
        self.assertEqual(
            self.state.setting_baseline[".obsidian/app.json"]["hash"],
            file_content_hash_binary(self.vault / ".obsidian" / "app.json"),
        )


if __name__ == "__main__":
    unittest.main()
