"""Tests for tying the incremental baseline to a server address and vault.

A baseline entry means "this server's vault already has this content". When
`server.api` or `server.vault` changes but the local tree and the state file
stay, an old baseline would tell the client that the new target has
everything, so nothing would ever be uploaded. The state therefore records a
short hash of the server/vault and the baseline is dropped when it changes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fns_cli.config import AppConfig
from fns_cli.state import SyncState, compute_baseline_scope
from fns_cli.sync_engine import SyncEngine


def _config(vault: Path, api: str = "http://fns.example:8080", name: str = "v1") -> AppConfig:
    config = AppConfig()
    config.sync.watch_path = str(vault)
    config.server.api = api
    config.server.vault = name
    return config


def _write_state(vault: Path, **fields) -> None:
    (vault / ".fns_state.json").write_text(json.dumps(fields), encoding="utf-8")


def _read_state(vault: Path) -> dict:
    return json.loads((vault / ".fns_state.json").read_text(encoding="utf-8"))


class TestComputeBaselineScope(unittest.TestCase):

    def test_trailing_slash_and_whitespace_are_ignored(self):
        self.assertEqual(
            compute_baseline_scope("  http://fns.example:8080/  ", "v1"),
            compute_baseline_scope("http://fns.example:8080", "v1"),
        )

    def test_address_or_vault_change_changes_the_scope(self):
        base = compute_baseline_scope("http://a", "v1")
        self.assertNotEqual(base, compute_baseline_scope("http://b", "v1"))
        self.assertNotEqual(base, compute_baseline_scope("http://a", "v2"))

    def test_scope_is_a_short_hex_digest_not_the_address(self):
        scope = compute_baseline_scope("http://fns.example:8080", "v1")
        self.assertEqual(len(scope), 16)
        int(scope, 16)
        self.assertNotIn("fns.example", scope)


class TestBaselineScopeOnStartup(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _seed(self, scope: str, content: bytes = b"x") -> None:
        (self.vault / "a.bin").write_bytes(content)
        _write_state(
            self.vault,
            last_file_sync_time=100,
            last_setting_sync_time=200,
            file_baseline={"a.bin": {"hash": "h", "mtime": 1, "size": 1}},
            setting_baseline={".obsidian/app.json": {"hash": "s", "mtime": 2, "size": 3}},
            baseline_scope=scope,
        )

    def test_unchanged_target_keeps_the_baseline(self):
        scope = compute_baseline_scope("http://fns.example:8080", "v1")
        self._seed(scope)

        engine = SyncEngine(_config(self.vault))

        self.assertEqual(engine.state.baseline_scope, scope)
        self.assertIn("a.bin", engine.state.file_baseline)
        self.assertIn(".obsidian/app.json", engine.state.setting_baseline)

    def test_changed_address_clears_both_baselines(self):
        self._seed(compute_baseline_scope("http://old.example", "v1"))

        engine = SyncEngine(_config(self.vault, api="http://new.example"))

        self.assertEqual(engine.state.file_baseline, {})
        self.assertEqual(engine.state.setting_baseline, {})
        self.assertEqual(
            engine.state.baseline_scope,
            compute_baseline_scope("http://new.example", "v1"),
        )
        # Cursors keep their previous behavior: they are not reset here.
        self.assertEqual(engine.state.last_file_sync_time, 100)
        self.assertEqual(engine.state.last_setting_sync_time, 200)
        self.assertEqual(_read_state(self.vault)["baseline_scope"],
                         engine.state.baseline_scope)

    def test_changed_vault_clears_the_baseline(self):
        self._seed(compute_baseline_scope("http://fns.example:8080", "v1"))

        engine = SyncEngine(_config(self.vault, name="v2"))

        self.assertEqual(engine.state.file_baseline, {})
        self.assertEqual(engine.state.setting_baseline, {})

    def test_legacy_state_without_scope_and_without_baseline_is_fine(self):
        _write_state(self.vault, last_file_sync_time=100)

        engine = SyncEngine(_config(self.vault))

        self.assertEqual(engine.state.file_baseline, {})
        self.assertEqual(engine.state.setting_baseline, {})
        self.assertEqual(
            engine.state.baseline_scope,
            compute_baseline_scope("http://fns.example:8080", "v1"),
        )

    def test_legacy_state_with_baseline_but_no_scope_is_cleared(self):
        (self.vault / "a.bin").write_bytes(b"x")
        _write_state(
            self.vault,
            last_file_sync_time=100,
            file_baseline={"a.bin": {"hash": "h", "mtime": 1, "size": 1}},
        )

        engine = SyncEngine(_config(self.vault))

        self.assertEqual(engine.state.file_baseline, {})
        self.assertEqual(engine.state.setting_baseline, {})
        self.assertTrue(engine.state.baseline_scope)

    async def test_cleared_baseline_reports_everything_next_round(self):
        (self.vault / "a.bin").write_bytes(b"x")
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        _write_state(
            self.vault,
            last_file_sync_time=100,
            last_setting_sync_time=200,
            file_baseline={"a.bin": {"hash": "h", "mtime": 1, "size": 1}},
            setting_baseline={
                ".obsidian/app.json": {"hash": "s", "mtime": 2, "size": 2},
            },
            baseline_scope=compute_baseline_scope("http://old.example", "v1"),
        )

        engine = SyncEngine(_config(self.vault))
        engine.ws_client = MagicMock()
        engine.ws_client.send = AsyncMock()

        await engine.file_sync.request_sync()
        file_msg = engine.ws_client.send.await_args.args[0]
        self.assertEqual([entry["path"] for entry in file_msg.data["files"]], ["a.bin"])
        self.assertEqual(file_msg.data["lastTime"], 100)

        await engine.setting_sync.request_sync()
        setting_msg = engine.ws_client.send.await_args.args[0]
        self.assertEqual(
            [entry["path"] for entry in setting_msg.data["settings"]],
            [".obsidian/app.json"],
        )
        self.assertEqual(setting_msg.data["lastTime"], 200)


if __name__ == "__main__":
    unittest.main()
