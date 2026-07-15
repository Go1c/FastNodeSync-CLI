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


if __name__ == "__main__":
    unittest.main()
