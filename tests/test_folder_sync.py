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
    ws.send = AsyncMock()

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


if __name__ == "__main__":
    unittest.main()
