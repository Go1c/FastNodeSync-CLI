"""Unit tests for config-directory collection rules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.protocol import WSMessage
from fns_cli.setting_sync import SettingSync


def _make_engine(vault_path: Path, *, sync_config: bool = True) -> MagicMock:
    config = MagicMock()
    config.server.vault = "test-vault"
    config.sync.config_sync_dirs = [".obsidian", ".agents"]
    config.sync.sync_config = sync_config

    state = MagicMock()
    state.last_setting_sync_time = 0

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


class TestSettingSyncConfigDirs(unittest.TestCase):

    def test_collect_local_settings_uses_config_dirs_when_sync_config_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / ".agents").mkdir()
            (vault / ".custom").mkdir()
            (vault / ".agents" / "rules.json").write_text("{}", encoding="utf-8")
            (vault / ".custom" / "state.json").write_text("{}", encoding="utf-8")

            sync = SettingSync(_make_engine(vault, sync_config=False))

            paths = {item["path"] for item in sync._collect_local_settings()}

        self.assertEqual(paths, {".agents/rules.json"})

    def test_collect_local_settings_includes_other_dot_dirs_when_sync_config_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / ".agents").mkdir()
            (vault / ".custom").mkdir()
            (vault / ".agents" / "rules.json").write_text("{}", encoding="utf-8")
            (vault / ".custom" / "state.json").write_text("{}", encoding="utf-8")

            sync = SettingSync(_make_engine(vault, sync_config=True))

            paths = {item["path"] for item in sync._collect_local_settings()}

        self.assertEqual(paths, {".agents/rules.json", ".custom/state.json"})


class TestSettingSyncInbound(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.engine = _make_engine(self.vault)
        self.sync = SettingSync(self.engine)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_upload_only_sync_handles_single_path_detail(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("offline edit", encoding="utf-8")
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 7890,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 1,
                "needSyncMtimeCount": 0,
            },
        }))
        self.assertEqual(self.engine.ws_client.sent[0].data["pageIndex"], -1)
        self.assertFalse(self.sync.is_sync_complete)
        self.assertEqual(self.engine.state.last_setting_sync_time, 0)

        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {"path": ".obsidian/app.json"},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(self.engine.state.last_setting_sync_time, 7890)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "SettingModify")

    async def test_legacy_upload_list_counts_each_detail(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("offline edit", encoding="utf-8")
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 8901,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 1,
                "needSyncMtimeCount": 0,
            },
        }))
        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {"needUpload": [{"path": ".obsidian/app.json"}]},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "SettingModify")

    async def test_mtime_only_sync_waits_for_mtime_detail(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("same", encoding="utf-8")
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 9012,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 0,
                "needSyncMtimeCount": 1,
            },
        }))
        self.assertFalse(self.sync.is_sync_complete)

        await self.sync._on_sync_mtime(WSMessage("SettingSyncMtime", {
            "data": {"path": ".obsidian/app.json", "mtime": 1000000},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(int(target.stat().st_mtime * 1000), 1000000)
        self.assertEqual(self.engine.state.last_setting_sync_time, 9012)

    async def test_upload_detail_without_path_still_counts(self):
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 8902,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 1,
                "needSyncMtimeCount": 0,
            },
        }))
        self.assertFalse(self.sync.is_sync_complete)

        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(self.engine.state.last_setting_sync_time, 8902)

    async def test_legacy_upload_list_without_paths_counts_once(self):
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 8903,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 1,
                "needSyncMtimeCount": 0,
            },
        }))
        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {"needUpload": [{}]},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(self.engine.state.last_setting_sync_time, 8903)

    async def test_upload_request_bypasses_echo_cache(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("cached", encoding="utf-8")
        # Prime the echo cache with the current on-disk hash, as a normal
        # watcher push would. Without force=True the upload would be skipped.
        await self.sync.push_modify(".obsidian/app.json")
        sent_before = len(self.engine.ws_client.sent)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "SettingModify")

        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {"path": ".obsidian/app.json"},
        }))
        self.assertEqual(len(self.engine.ws_client.sent), sent_before + 1)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "SettingModify")
        self.assertEqual(self.engine.ws_client.sent[-1].data["content"], "cached")

    async def test_legacy_upload_list_bypasses_echo_cache(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("cached", encoding="utf-8")
        await self.sync.push_modify(".obsidian/app.json")
        sent_before = len(self.engine.ws_client.sent)

        await self.sync._on_sync_need_upload(WSMessage("SettingSyncNeedUpload", {
            "data": {"needUpload": [{"path": ".obsidian/app.json"}]},
        }))
        self.assertEqual(len(self.engine.ws_client.sent), sent_before + 1)
        self.assertEqual(self.engine.ws_client.sent[-1].action, "SettingModify")

    async def test_zero_mtime_detail_still_completes_round(self):
        target = self.vault / ".obsidian" / "app.json"
        target.parent.mkdir()
        target.write_text("same", encoding="utf-8")
        before = target.stat().st_mtime
        await self.sync._on_sync_end(WSMessage("SettingSyncEnd", {
            "data": {
                "lastTime": 9013,
                "needModifyCount": 0,
                "needDeleteCount": 0,
                "needUploadCount": 0,
                "needSyncMtimeCount": 1,
            },
        }))
        self.assertFalse(self.sync.is_sync_complete)

        await self.sync._on_sync_mtime(WSMessage("SettingSyncMtime", {
            "data": {"path": ".obsidian/app.json", "mtime": 0},
        }))
        self.assertTrue(self.sync.is_sync_complete)
        self.assertEqual(self.engine.state.last_setting_sync_time, 9013)
        self.assertEqual(target.stat().st_mtime, before)

    async def test_setting_sync_pagination_flow(self):
        self.sync.register_handlers()

        end_msg = WSMessage(
            "SettingSyncEnd",
            {
                "context": "ctx-set",
                "vault": "vault-B",
                "data": {"lastTime": 2000, "needModifyCount": 1, "needDeleteCount": 0},
            },
        )
        await self.sync._on_sync_end(end_msg)

        self.assertEqual(len(self.engine.ws_client.sent), 1)
        self.assertEqual(self.engine.ws_client.sent[0].action, "SettingSyncPageAck")
        self.assertEqual(
            self.engine.ws_client.sent[0].data,
            {"context": "ctx-set", "pageIndex": -1, "vault": "vault-B"},
        )

        page_msg = WSMessage(
            "SettingSyncPage",
            {"data": {"pageIndex": 0, "isLast": False, "items": []}},
        )
        await self.sync._on_sync_page(page_msg)
        self.assertEqual(len(self.engine.ws_client.sent), 2)
        self.assertEqual(self.engine.ws_client.sent[1].data["pageIndex"], 0)


if __name__ == "__main__":
    unittest.main()
