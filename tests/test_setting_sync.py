"""Unit tests for config-directory collection rules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fns_cli.setting_sync import SettingSync


def _make_engine(vault_path: Path, *, sync_config: bool) -> MagicMock:
    config = MagicMock()
    config.sync.config_sync_dirs = [".obsidian", ".agents"]
    config.sync.sync_config = sync_config

    engine = MagicMock()
    engine.config = config
    engine.vault_path = vault_path
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


if __name__ == "__main__":
    unittest.main()
