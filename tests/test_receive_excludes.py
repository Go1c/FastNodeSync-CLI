"""Receive-side exclusion tests.

Kept in a dedicated module so this change does not append to the shared
test files, which other branches also modify.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fns_cli.file_sync import FileSync
from fns_cli.folder_sync import FolderSync
from fns_cli.note_sync import NoteSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_CHUNK_DOWNLOAD,
    ACTION_FILE_SYNC_DELETE,
    ACTION_FILE_SYNC_UPDATE,
    ACTION_NOTE_SYNC_MODIFY,
    ACTION_NOTE_SYNC_RENAME,
    WSMessage,
)
from fns_cli.setting_sync import SettingSync

from test_file_sync import _make_engine as _make_file_engine
from test_file_sync import _wrap
from test_folder_sync import _make_engine as _make_folder_engine
from test_note_sync import _make_engine as _make_note_engine
from test_setting_sync import _make_engine as _make_setting_engine


class TestFileSyncExcludes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_file_engine(self.vault)
        self.engine.is_excluded = MagicMock(
            side_effect=lambda path: path.startswith(".trash/")
        )
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_excluded_update_neither_downloads_nor_writes(self):
        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": ".trash/new.bin", "content": None,
        }))
        self.assertFalse((self.vault / ".trash" / "new.bin").exists())
        self.engine.ws_client.send.assert_not_awaited()
        self.assertEqual(self.fs._received_modify, 1)

        await self.fs._on_sync_update(_wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": ".trash/text.bin", "content": "remote",
        }))
        self.assertFalse((self.vault / ".trash" / "text.bin").exists())
        self.assertEqual(self.fs._received_modify, 2)

    async def test_excluded_delete_and_rename_preserve_local_files(self):
        old = self.vault / "folder" / "old.bin"
        old.parent.mkdir()
        old.write_bytes(b"local")
        trash = self.vault / ".trash" / "keep.bin"
        trash.parent.mkdir()
        trash.write_bytes(b"local")

        await self.fs._on_sync_delete(_wrap(ACTION_FILE_SYNC_DELETE, {
            "path": ".trash/keep.bin",
        }))
        self.assertTrue(trash.exists())
        self.assertEqual(self.fs._received_delete, 1)

        await self.fs._on_sync_rename(_wrap("FileSyncRename", {
            "oldPath": "folder/old.bin", "path": ".trash/new.bin",
        }))
        self.assertTrue(old.exists())
        self.assertFalse((self.vault / ".trash" / "new.bin").exists())

    async def test_excluded_chunk_download_is_not_written(self):
        local = self.vault / ".trash" / "chunked.bin"
        local.parent.mkdir()
        local.write_bytes(b"local")
        await self.fs._on_chunk_download_start(_wrap(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, {
            "sessionId": "12345678-1234-1234-1234-123456789012",
            "path": ".trash/chunked.bin", "size": 6,
            "totalChunks": 1, "chunkSize": 6,
        }))
        await self.fs._on_binary_chunk(
            "12345678-1234-1234-1234-123456789012", 0, b"remote",
        )
        self.assertEqual(local.read_bytes(), b"local")

    async def test_excluded_mtime_is_not_applied(self):
        excluded = self.vault / ".trash" / "keep.bin"
        excluded.parent.mkdir()
        excluded.write_bytes(b"local")
        os.utime(excluded, (1_000_000, 1_000_000))
        await self.fs._on_sync_mtime(_wrap("FileSyncMtime", {
            "path": ".trash/keep.bin", "mtime": 2_000_000_000_000,
        }))
        self.assertEqual(int(excluded.stat().st_mtime), 1_000_000)

        included = self.vault / "notes" / "keep.bin"
        included.parent.mkdir()
        included.write_bytes(b"local")
        os.utime(included, (1_000_000, 1_000_000))
        await self.fs._on_sync_mtime(_wrap("FileSyncMtime", {
            "path": "notes/keep.bin", "mtime": 2_000_000_000_000,
        }))
        self.assertEqual(int(included.stat().st_mtime), 2_000_000_000)


class TestNoteSyncExcludes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_note_engine(self.vault)
        self.engine.is_excluded = MagicMock(
            side_effect=lambda path: path.startswith(".trash/")
        )
        self.ns = NoteSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_excluded_messages_skip_disk_changes_and_count(self):
        excluded = self.vault / ".trash" / "keep.md"
        excluded.parent.mkdir()
        excluded.write_text("local", encoding="utf-8")

        await self.ns._on_sync_modify(_wrap(ACTION_NOTE_SYNC_MODIFY, {
            "path": ".trash/new.md", "content": "remote",
        }))
        self.assertFalse((self.vault / ".trash" / "new.md").exists())
        self.assertEqual(self.ns._received_modify, 1)

        await self.ns._on_sync_delete(_wrap("NoteSyncDelete", {"path": ".trash/keep.md"}))
        self.assertEqual(excluded.read_text(encoding="utf-8"), "local")
        self.assertEqual(self.ns._received_delete, 1)

        old = self.vault / "folder" / "old.md"
        old.parent.mkdir()
        old.write_text("local", encoding="utf-8")
        await self.ns._on_sync_rename(_wrap(ACTION_NOTE_SYNC_RENAME, {
            "oldPath": "folder/old.md", "path": ".trash/new.md",
        }))
        self.assertTrue(old.exists())
        self.assertFalse((self.vault / ".trash" / "new.md").exists())

    async def test_excluded_mtime_is_not_applied(self):
        excluded = self.vault / ".trash" / "keep.md"
        excluded.parent.mkdir()
        excluded.write_text("local", encoding="utf-8")
        os.utime(excluded, (1_000_000, 1_000_000))
        await self.ns._on_sync_mtime(_wrap("NoteSyncMtime", {
            "path": ".trash/keep.md", "mtime": 2_000_000_000_000,
        }))
        self.assertEqual(int(excluded.stat().st_mtime), 1_000_000)

        included = self.vault / "notes" / "keep.md"
        included.parent.mkdir()
        included.write_text("local", encoding="utf-8")
        os.utime(included, (1_000_000, 1_000_000))
        await self.ns._on_sync_mtime(_wrap("NoteSyncMtime", {
            "path": "notes/keep.md", "mtime": 2_000_000_000_000,
        }))
        self.assertEqual(int(included.stat().st_mtime), 2_000_000_000)


class TestSettingSyncExcludes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_setting_engine(self.vault)
        self.engine.is_excluded = MagicMock(
            side_effect=lambda path: path.startswith(".trash/")
        )
        self.sync = SettingSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_excluded_messages_skip_disk_changes_and_count(self):
        await self.sync._on_sync_modify(WSMessage("SettingSyncModify", {
            "data": {"path": ".trash/new.json", "content": "remote"},
        }))
        self.assertFalse((self.vault / ".trash" / "new.json").exists())
        self.assertEqual(self.sync._received_modify, 1)

        keep = self.vault / ".trash" / "keep.json"
        keep.parent.mkdir()
        keep.write_text("local", encoding="utf-8")
        await self.sync._on_sync_delete(WSMessage("SettingSyncDelete", {
            "data": {"path": ".trash/keep.json"},
        }))
        self.assertTrue(keep.exists())
        self.assertEqual(self.sync._received_delete, 1)

        old = self.vault / ".obsidian" / "old.json"
        old.parent.mkdir()
        old.write_text("local", encoding="utf-8")
        await self.sync._on_sync_rename(WSMessage("SettingSyncRename", {
            "data": {"oldPath": ".obsidian/old.json", "path": ".trash/new.json"},
        }))
        self.assertTrue(old.exists())
        self.assertFalse((self.vault / ".trash" / "new.json").exists())

    async def test_excluded_mtime_is_not_applied(self):
        excluded = self.vault / ".trash" / "keep.json"
        excluded.parent.mkdir()
        excluded.write_text("{}", encoding="utf-8")
        os.utime(excluded, (1_000_000, 1_000_000))
        await self.sync._on_sync_mtime(WSMessage("SettingSyncMtime", {
            "data": {"path": ".trash/keep.json", "mtime": 2_000_000_000_000},
        }))
        self.assertEqual(int(excluded.stat().st_mtime), 1_000_000)

        included = self.vault / ".obsidian" / "keep.json"
        included.parent.mkdir()
        included.write_text("{}", encoding="utf-8")
        os.utime(included, (1_000_000, 1_000_000))
        await self.sync._on_sync_mtime(WSMessage("SettingSyncMtime", {
            "data": {"path": ".obsidian/keep.json", "mtime": 2_000_000_000_000},
        }))
        self.assertEqual(int(included.stat().st_mtime), 2_000_000_000)


class TestFolderSyncExcludes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_folder_engine(self.vault)
        self.engine.is_excluded = MagicMock(return_value=False)
        self.fs = FolderSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_excluded_folder_events_skip_disk_changes_and_count(self):
        self.engine.is_excluded = lambda path: path.startswith(".trash/")
        await self.fs._on_sync_modify(_wrap("FolderSyncModify", {
            "path": ".trash/new",
        }))
        self.assertFalse((self.vault / ".trash" / "new").exists())
        self.assertEqual(self.fs._received_modify, 1)

        keep = self.vault / ".trash" / "keep"
        keep.mkdir(parents=True)
        await self.fs._on_sync_delete(_wrap("FolderSyncDelete", {
            "path": ".trash/keep",
        }))
        self.assertTrue(keep.exists())
        self.assertEqual(self.fs._received_delete, 1)

        old = self.vault / "folder" / "old"
        old.mkdir(parents=True)
        await self.fs._on_sync_rename(_wrap("FolderSyncRename", {
            "oldPath": "folder/old", "path": ".trash/new",
        }))
        self.assertTrue(old.exists())
        self.assertFalse((self.vault / ".trash" / "new").exists())

    async def test_delete_removes_whole_tree_when_nothing_excluded(self):
        root = self.vault / "notes"
        (root / "sub").mkdir(parents=True)
        (root / "a.md").write_text("a", encoding="utf-8")
        (root / "sub" / "b.bin").write_bytes(b"b")
        await self.fs._on_sync_delete(_wrap("FolderSyncDelete", {"path": "notes"}))
        self.assertFalse(root.exists())
        self.assertEqual(self.fs._received_delete, 1)

    async def test_delete_keeps_excluded_files_and_parent_dirs(self):
        self.engine.is_excluded = lambda path: path.endswith(".tmp")
        root = self.vault / "notes"
        (root / "sub").mkdir(parents=True)
        (root / "keep.md").write_text("keep", encoding="utf-8")
        (root / "a.tmp").write_bytes(b"local")
        (root / "sub" / "b.md").write_text("b", encoding="utf-8")
        (root / "sub" / "c.tmp").write_bytes(b"local")

        await self.fs._on_sync_delete(_wrap("FolderSyncDelete", {"path": "notes"}))

        self.assertTrue(root.is_dir())
        self.assertTrue((root / "sub").is_dir())
        self.assertEqual((root / "a.tmp").read_bytes(), b"local")
        self.assertEqual((root / "sub" / "c.tmp").read_bytes(), b"local")
        self.assertFalse((root / "keep.md").exists())
        self.assertFalse((root / "sub" / "b.md").exists())
        self.assertEqual(self.fs._received_delete, 1)

    async def test_delete_unlinks_symlinks_without_following_them(self):
        outside = self.vault / "outside"
        outside.mkdir()
        (outside / "target.txt").write_text("keep", encoding="utf-8")

        root = self.vault / "notes"
        root.mkdir()
        (root / "file.txt").write_text("f", encoding="utf-8")
        (root / "link_dir").symlink_to(outside, target_is_directory=True)
        (root / "link_file").symlink_to(outside / "target.txt")

        await self.fs._on_sync_delete(_wrap("FolderSyncDelete", {"path": "notes"}))

        self.assertFalse(root.exists())
        self.assertTrue((outside / "target.txt").exists())
        self.assertEqual(self.fs._received_delete, 1)


if __name__ == "__main__":
    unittest.main()
