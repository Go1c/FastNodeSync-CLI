"""Unit tests for FolderSync server-pushed folder events."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.folder_sync import FolderSync
from fns_cli.protocol import WSMessage


def _make_engine(vault_path: Path) -> MagicMock:
    config = MagicMock()
    ws = MagicMock()
    ws.sent = []
    async def _send(msg):
        ws.sent.append(msg)
    ws.send = AsyncMock(side_effect=_send)

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault_path
    engine.ws_client = ws
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    return WSMessage(action, {"data": inner})


class TestFolderSync(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.fs = FolderSync(_make_engine(self.vault))

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_modify_creates_folder(self):
        await self.fs._on_sync_modify(_wrap("FolderSyncModify", {"path": "foo/bar"}))
        self.assertTrue((self.vault / "foo" / "bar").is_dir())

    async def test_rename_moves_folder(self):
        old_dir = self.vault / "foo" / "bar"
        old_dir.mkdir(parents=True)
        (old_dir / "note.md").write_text("x", encoding="utf-8")

        await self.fs._on_sync_rename(_wrap("FolderSyncRename", {
            "oldPath": "foo/bar",
            "path": "foo/baz",
        }))

        self.assertFalse((self.vault / "foo" / "bar").exists())
        self.assertTrue((self.vault / "foo" / "baz" / "note.md").exists())

    async def test_delete_removes_folder_tree(self):
        doomed = self.vault / "foo" / "bar"
        doomed.mkdir(parents=True)
        (doomed / "note.md").write_text("x", encoding="utf-8")

        await self.fs._on_sync_delete(_wrap("FolderSyncDelete", {"path": "foo"}))

        self.assertFalse((self.vault / "foo").exists())

    async def test_folder_sync_pagination_flow(self):
        self.fs.register_handlers()

        end_msg = WSMessage(
            "FolderSyncEnd",
            {
                "context": "ctx-folder",
                "vault": "vault-D",
                "data": {"needModifyCount": 1, "needDeleteCount": 1},
            },
        )
        await self.fs._on_sync_end(end_msg)

        self.assertEqual(len(self.fs.engine.ws_client.sent), 1)
        self.assertEqual(self.fs.engine.ws_client.sent[0].action, "FolderSyncPageAck")
        self.assertEqual(self.fs.engine.ws_client.sent[0].data, {"context": "ctx-folder", "pageIndex": -1, "vault": "vault-D"})
        self.assertFalse(self.fs.is_sync_complete)

        # Simulate 1 modify and 1 delete
        await self.fs._on_sync_modify(WSMessage("FolderSyncModify", {"path": "folderA"}))
        await self.fs._on_sync_delete(WSMessage("FolderSyncDelete", {"path": "folderB"}))
        self.assertEqual(self.fs._received_modify, 1)
        self.assertEqual(self.fs._received_delete, 1)
        self.assertTrue(self.fs.is_sync_complete)

    async def test_sync_end_zero_counts_completes_immediately(self):
        self.fs.register_handlers()
        end_msg = WSMessage(
            "FolderSyncEnd",
            {
                "context": "ctx-empty",
                "vault": "vault-D",
                "data": {"needModifyCount": 0, "needDeleteCount": 0},
            },
        )
        await self.fs._on_sync_end(end_msg)
        self.assertTrue(self.fs.is_sync_complete)
        self.assertEqual(len(self.fs.engine.ws_client.sent), 0)

    async def test_sync_page_ack_and_last_page(self):
        self.fs.register_handlers()
        end_msg = WSMessage(
            "FolderSyncEnd",
            {
                "context": "ctx-page",
                "vault": "vault-D",
                "data": {"needModifyCount": 1, "needDeleteCount": 0},
            },
        )
        await self.fs._on_sync_end(end_msg)
        self.assertEqual(len(self.fs.engine.ws_client.sent), 1)
        self.assertEqual(self.fs.engine.ws_client.sent[0].data["pageIndex"], -1)

        page_msg = WSMessage(
            "FolderSyncPage",
            {
                "context": "ctx-page",
                "pageIndex": 0,
                "data": {"pageIndex": 0, "isLast": False},
            },
        )
        await self.fs._on_sync_page(page_msg)
        self.assertEqual(len(self.fs.engine.ws_client.sent), 2)
        self.assertEqual(self.fs.engine.ws_client.sent[1].data["pageIndex"], 0)
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs._on_sync_modify(WSMessage("FolderSyncModify", {"path": "folderC"}))
        last_page_msg = WSMessage(
            "FolderSyncPage",
            {
                "context": "ctx-page",
                "pageIndex": 1,
                "data": {"pageIndex": 1, "isLast": True},
            },
        )
        await self.fs._on_sync_page(last_page_msg)
        self.assertTrue(self.fs.is_sync_complete)

    async def test_reset_counters(self):
        self.fs._sync_complete = True
        self.fs._got_end = True
        self.fs._expected_modify = 5
        self.fs._received_modify = 5
        self.fs._sync_context = "ctx"
        self.fs._sync_vault = "vault"

        self.fs._reset_counters()
        self.assertFalse(self.fs.is_sync_complete)
        self.assertFalse(self.fs._got_end)
        self.assertEqual(self.fs._expected_modify, 0)
        self.assertEqual(self.fs._received_modify, 0)
        self.assertEqual(self.fs._sync_context, "")
        self.assertEqual(self.fs._sync_vault, "")

    async def test_excluded_folder_events_skip_disk_changes_and_count(self):
        self.fs.engine.is_excluded = lambda path: path.startswith(".trash/")
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
        self.fs.engine.is_excluded = lambda path: path.endswith(".tmp")
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

    async def test_config_dir_events_increment_counters_and_complete_sync(self):
        self.fs.register_handlers()
        self.fs.engine.config.sync.config_sync_dirs = [".obsidian"]

        end_msg = WSMessage(
            "FolderSyncEnd",
            {
                "context": "ctx-cfg",
                "vault": "vault-D",
                "data": {"needModifyCount": 1, "needDeleteCount": 1},
            },
        )
        await self.fs._on_sync_end(end_msg)
        self.assertFalse(self.fs.is_sync_complete)

        await self.fs._on_sync_modify(WSMessage("FolderSyncModify", {"path": ".obsidian/plugins/foo"}))
        self.assertEqual(self.fs._received_modify, 1)
        self.assertFalse(self.fs.is_sync_complete)
        self.assertFalse((self.vault / ".obsidian/plugins/foo").exists())

        await self.fs._on_sync_delete(WSMessage("FolderSyncDelete", {"path": ".obsidian/themes/bar"}))
        self.assertEqual(self.fs._received_delete, 1)
        self.assertTrue(self.fs.is_sync_complete)


if __name__ == "__main__":
    unittest.main()

