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
