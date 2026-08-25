"""Unit tests for NoteSync inbound state handling and rename behaviour."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.note_sync import NoteSync
from fns_cli.protocol import (
    ACTION_NOTE_SYNC_END,
    ACTION_NOTE_SYNC_MODIFY,
    ACTION_NOTE_SYNC_RENAME,
    WSMessage,
)


def _make_engine(vault_path: Path) -> MagicMock:
    config = MagicMock()
    config.server.vault = "test-vault"

    state = MagicMock()
    state.last_note_sync_time = 0

    ws = MagicMock()
    ws.sent = []
    async def _send(msg):
        ws.sent.append(msg)
    ws.send = AsyncMock(side_effect=_send)

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault_path
    engine.state = state
    engine.ws_client = ws
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    return WSMessage(action, {"data": inner})


class TestNoteSyncInbound(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.ns = NoteSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_last_time_commits_only_after_all_details_arrive(self):
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 1234,
            "needModifyCount": 1,
            "needDeleteCount": 0,
            "needUploadCount": 0,
        }))
        self.assertEqual(self.engine.state.last_note_sync_time, 0)

        await self.ns._on_sync_modify(_wrap(ACTION_NOTE_SYNC_MODIFY, {
            "path": "note.md",
            "content": "hello",
            "mtime": 0,
        }))

        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 1234)
        self.engine.state.save.assert_called()

    async def test_sync_rename_moves_note_on_disk(self):
        old_path = self.vault / "folder" / "old.md"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text("hello", encoding="utf-8")

        await self.ns._on_sync_rename(_wrap(ACTION_NOTE_SYNC_RENAME, {
            "oldPath": "folder/old.md",
            "path": "folder/new.md",
        }))

        self.assertFalse((self.vault / "folder" / "old.md").exists())
        self.assertEqual((self.vault / "folder" / "new.md").read_text(encoding="utf-8"), "hello")

    async def test_note_sync_pagination_flow(self):
        self.ns.register_handlers()

        # 1. Simulate NoteSyncEnd with expected items
        end_msg = WSMessage(
            "NoteSyncEnd",
            {
                "context": "ctx-123",
                "vault": "vault-A",
                "data": {
                    "lastTime": 1000,
                    "needModifyCount": 1,
                    "needDeleteCount": 0,
                },
            },
        )
        await self.ns._on_sync_end(end_msg)

        # Verify initial PageAck(pageIndex=-1) was sent
        self.assertEqual(len(self.engine.ws_client.sent), 1)
        sent_ack = self.engine.ws_client.sent[0]
        self.assertEqual(sent_ack.action, "NoteSyncPageAck")
        self.assertEqual(sent_ack.data, {"context": "ctx-123", "pageIndex": -1, "vault": "vault-A"})
        self.assertFalse(self.ns.is_sync_complete)

        # 2. Simulate NoteSyncPage(pageIndex=0, isLast=False)
        page_msg = WSMessage(
            "NoteSyncPage",
            {
                "context": "ctx-123",
                "pageIndex": 0,
                "data": {"pageIndex": 0, "isLast": False, "items": ["note1.md"]},
            },
        )
        await self.ns._on_sync_page(page_msg)

        # Verify PageAck(pageIndex=0) sent
        self.assertEqual(len(self.engine.ws_client.sent), 2)
        self.assertEqual(self.engine.ws_client.sent[1].data["pageIndex"], 0)
        self.assertFalse(self.ns.is_sync_complete)

        # 3. Simulate Modify arrival and NoteSyncPage(isLast=True)
        self.ns._received_modify = 1
        last_page_msg = WSMessage(
            "NoteSyncPage",
            {
                "context": "ctx-123",
                "pageIndex": 1,
                "data": {"pageIndex": 1, "isLast": True, "items": []},
            },
        )
        await self.ns._on_sync_page(last_page_msg)
        self.assertTrue(self.ns.is_sync_complete)


if __name__ == "__main__":
    unittest.main()
