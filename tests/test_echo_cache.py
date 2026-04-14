"""Regression tests for the content-hash echo cache.

The cache must track "this path's most recently announced state" — updated on
both inbound (server → local) and outbound (local → server) transitions —
so legitimate user edits that happen to *restore* a previous value still get
pushed.

Two scenarios cover the bugs that shipped in the inbound-only version:

  1. receive A  → push B  → revert to A
     Inbound-only cache would still read A after step 2, so the A revert in
     step 3 would be treated as an echo and dropped.

  2. receive delete → recreate → delete again
     Inbound-only tombstone would persist, so the final delete would be
     dropped.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fns_cli.file_sync import FileSync
from fns_cli.note_sync import NoteSync
from fns_cli.protocol import (
    ACTION_FILE_SYNC_DELETE,
    ACTION_FILE_SYNC_UPDATE,
    ACTION_NOTE_SYNC_DELETE,
    ACTION_NOTE_SYNC_MODIFY,
    ACTION_NOTE_SYNC_NEED_PUSH,
    WSMessage,
)


def _make_engine(vault_path: Path) -> MagicMock:
    config = MagicMock()
    config.server.vault = "test-vault"
    config.sync.file_chunk_size = 1024 * 1024

    state = MagicMock()
    state.last_note_sync_time = 0
    state.last_file_sync_time = 0

    ws = MagicMock()
    ws.send = AsyncMock()

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault_path
    engine.state = state
    engine.ws_client = ws
    engine.ignore_file = MagicMock()
    engine.unignore_file = MagicMock()
    engine.is_excluded = MagicMock(return_value=False)
    return engine


def _wrap(action: str, inner: dict) -> WSMessage:
    return WSMessage(action, {"data": inner})


def _sent_actions(engine: MagicMock) -> list[str]:
    """Extract the Action name of each WSMessage passed to ws.send."""
    return [call.args[0].action for call in engine.ws_client.send.await_args_list]


class TestNoteEchoCache(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.ns = NoteSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _inbound_modify(self, rel: str, content: str) -> None:
        msg = _wrap(ACTION_NOTE_SYNC_MODIFY, {
            "path": rel, "content": content, "mtime": 0,
        })
        await self.ns._on_sync_modify(msg)

    async def _inbound_delete(self, rel: str) -> None:
        msg = _wrap(ACTION_NOTE_SYNC_DELETE, {"path": rel})
        await self.ns._on_sync_delete(msg)

    async def test_revert_after_push_still_pushes(self):
        """receive A → push B → revert to A: the revert must be pushed."""
        rel = "note.md"
        target = self.vault / rel

        # 1. Server hands us A.
        await self._inbound_modify(rel, "A")
        self.assertEqual(target.read_text(encoding="utf-8"), "A")

        # 2. User edits locally to B and we push.
        target.write_text("B", encoding="utf-8")
        await self.ns.push_modify(rel)

        # 3. User reverts to A. The cache must NOT suppress this.
        target.write_text("A", encoding="utf-8")
        await self.ns.push_modify(rel)

        actions = _sent_actions(self.engine)
        self.assertEqual(
            actions.count("NoteModify"), 2,
            f"expected two NoteModify pushes (B and A), got {actions}",
        )

    async def test_delete_after_recreate_still_pushes(self):
        """receive delete → local recreate → local delete: last delete must be pushed."""
        rel = "note.md"
        target = self.vault / rel
        target.write_text("initial", encoding="utf-8")

        # 1. Server tells us to delete.
        await self._inbound_delete(rel)
        self.assertFalse(target.exists())

        # 2. User recreates and we push.
        target.write_text("fresh", encoding="utf-8")
        await self.ns.push_modify(rel)

        # 3. User deletes again locally.
        target.unlink()
        await self.ns.push_delete(rel)

        actions = _sent_actions(self.engine)
        self.assertIn("NoteModify", actions)
        self.assertEqual(
            actions.count("NoteDelete"), 1,
            f"expected the final delete to be pushed, got {actions}",
        )

    async def test_need_push_bypasses_echo_suppression(self):
        """NeedPush is an explicit server request and must force a re-send."""
        rel = "note.md"

        await self._inbound_modify(rel, "A")
        await self.ns._on_sync_need_push(
            _wrap(ACTION_NOTE_SYNC_NEED_PUSH, {"path": rel})
        )

        actions = _sent_actions(self.engine)
        self.assertEqual(
            actions.count("NoteModify"), 1,
            f"expected NeedPush to force a NoteModify, got {actions}",
        )

    async def test_failed_inbound_modify_does_not_poison_cache(self):
        """A failed server write must not suppress the next real local push."""
        rel = "note.md"
        target = self.vault / rel

        with patch.object(Path, "write_text", side_effect=OSError("boom")):
            await self._inbound_modify(rel, "A")

        self.assertNotIn(rel, self.ns._echo_hashes)

        target.write_text("A", encoding="utf-8")
        await self.ns.push_modify(rel)

        actions = _sent_actions(self.engine)
        self.assertEqual(
            actions.count("NoteModify"), 1,
            f"expected local push after failed inbound write, got {actions}",
        )

    async def test_failed_inbound_delete_does_not_poison_cache(self):
        """A failed server delete must not suppress a later real local delete."""
        rel = "note.md"
        target = self.vault / rel
        target.write_text("A", encoding="utf-8")

        with patch.object(Path, "unlink", side_effect=OSError("boom")):
            await self._inbound_delete(rel)

        self.assertNotEqual(self.ns._echo_hashes.get(rel), "_DELETED")

        target.unlink()
        await self.ns.push_delete(rel)

        actions = _sent_actions(self.engine)
        self.assertEqual(
            actions.count("NoteDelete"), 1,
            f"expected local delete after failed inbound delete, got {actions}",
        )


class TestFileEchoCache(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.fs = FileSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _inbound_update_text(self, rel: str, content: str) -> None:
        msg = _wrap(ACTION_FILE_SYNC_UPDATE, {
            "path": rel, "content": content, "mtime": 0,
        })
        await self.fs._on_sync_update(msg)

    async def _inbound_delete(self, rel: str) -> None:
        msg = _wrap(ACTION_FILE_SYNC_DELETE, {"path": rel})
        await self.fs._on_sync_delete(msg)

    async def test_revert_after_push_still_pushes(self):
        rel = "config.json"
        target = self.vault / rel

        await self._inbound_update_text(rel, "A")
        self.assertEqual(target.read_text(encoding="utf-8"), "A")

        target.write_text("B", encoding="utf-8")
        await self.fs.push_upload(rel)

        target.write_text("A", encoding="utf-8")
        await self.fs.push_upload(rel)

        actions = _sent_actions(self.engine)
        self.assertEqual(
            actions.count("FileUploadCheck"), 2,
            f"expected two FileUploadCheck pushes, got {actions}",
        )

    async def test_delete_after_recreate_still_pushes(self):
        rel = "pic.png"
        target = self.vault / rel
        target.write_bytes(b"initial")

        await self._inbound_delete(rel)
        self.assertFalse(target.exists())

        target.write_bytes(b"fresh")
        await self.fs.push_upload(rel)

        target.unlink()
        await self.fs.push_delete(rel)

        actions = _sent_actions(self.engine)
        self.assertIn("FileUploadCheck", actions)
        self.assertEqual(
            actions.count("FileDelete"), 1,
            f"expected the final delete to be pushed, got {actions}",
        )


if __name__ == "__main__":
    unittest.main()
