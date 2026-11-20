"""Detail counting for NoteSync: upload/mtime details advance the round."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fns_cli.note_sync import NoteSync
from fns_cli.protocol import ACTION_NOTE_SYNC_END

from test_note_sync import _make_engine, _wrap


class TestDetailCountingNote(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.ns = NoteSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_upload_only_sync_waits_for_need_push_detail(self):
        target = self.vault / "offline.md"
        target.write_text("offline edit", encoding="utf-8")
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 3456,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))

        self.assertEqual(self.engine.ws_client.sent[0].data["pageIndex"], -1)
        self.assertFalse(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 0)

        await self.ns._on_sync_need_push(_wrap("NoteSyncNeedPush", {"path": "offline.md"}))
        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 3456)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "NoteModify")

    async def test_mtime_only_sync_commits_after_mtime_detail(self):
        target = self.vault / "note.md"
        target.write_text("same", encoding="utf-8")
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 4567,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 1,
        }))
        self.assertEqual(self.engine.ws_client.sent[0].action, "NoteSyncPageAck")
        self.assertEqual(self.engine.ws_client.sent[0].data["pageIndex"], -1)
        self.assertFalse(self.ns.is_sync_complete)

        await self.ns._on_sync_mtime(_wrap("NoteSyncMtime", {
            "path": "note.md", "mtime": 1000000,
        }))
        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(int(target.stat().st_mtime * 1000), 1000000)
        self.assertEqual(self.engine.state.last_note_sync_time, 4567)

    async def test_need_push_without_path_still_counts(self):
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 3457,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        self.assertFalse(self.ns.is_sync_complete)

        await self.ns._on_sync_need_push(_wrap("NoteSyncNeedPush", {}))
        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 3457)

    async def test_need_push_missing_file_still_counts(self):
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 3458,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))
        await self.ns._on_sync_need_push(_wrap("NoteSyncNeedPush", {"path": "gone.md"}))
        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 3458)
        self.assertNotIn("NoteModify", [m.action for m in self.engine.ws_client.sent])

    async def test_need_push_outside_vault_is_not_sent_but_counts(self):
        outside = self.vault.parent / "outside-note.md"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 3459,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 1,
            "needSyncMtimeCount": 0,
        }))

        with self.assertLogs("fns_cli.note_sync", level="WARNING"):
            await self.ns._on_sync_need_push(_wrap("NoteSyncNeedPush", {
                "path": "../outside-note.md",
            }))

        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 3459)
        self.assertNotIn("NoteModify", [m.action for m in self.engine.ws_client.sent])

    async def test_need_push_outside_round_does_not_repeat_complete(self):
        target = self.vault / "later.md"
        target.write_text("edit", encoding="utf-8")
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 3460,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 0,
        }))
        self.assertTrue(self.ns.is_sync_complete)
        received_before = self.ns._received_upload

        with self.assertLogs("fns_cli.note_sync", level="INFO") as logs:
            await self.ns._on_sync_need_push(_wrap("NoteSyncNeedPush", {"path": "later.md"}))
        self.assertFalse(any("NoteSync complete" in line for line in logs.output), logs.output)

        self.assertEqual(self.ns._received_upload, received_before)
        self.assertEqual(self.engine.state.last_note_sync_time, 3460)

    async def test_zero_mtime_detail_still_completes_round(self):
        target = self.vault / "note.md"
        target.write_text("same", encoding="utf-8")
        before = target.stat().st_mtime
        await self.ns._on_sync_end(_wrap(ACTION_NOTE_SYNC_END, {
            "lastTime": 4568,
            "needModifyCount": 0,
            "needDeleteCount": 0,
            "needUploadCount": 0,
            "needSyncMtimeCount": 1,
        }))
        self.assertFalse(self.ns.is_sync_complete)

        await self.ns._on_sync_mtime(_wrap("NoteSyncMtime", {
            "path": "note.md", "mtime": 0,
        }))
        self.assertTrue(self.ns.is_sync_complete)
        self.assertEqual(self.engine.state.last_note_sync_time, 4568)
        self.assertEqual(target.stat().st_mtime, before)

if __name__ == "__main__":
    unittest.main()
