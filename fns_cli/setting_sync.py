"""Setting (config) sync protocol: SettingSync incremental pull + SettingModify/SettingDelete push."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .hash_utils import file_content_hash_binary, path_hash
from .protocol import (
    ACTION_SETTING_DELETE,
    ACTION_SETTING_MODIFY,
    ACTION_SETTING_SYNC,
    ACTION_SETTING_SYNC_DELETE,
    ACTION_SETTING_SYNC_END,
    ACTION_SETTING_SYNC_MODIFY,
    ACTION_SETTING_SYNC_MTIME,
    ACTION_SETTING_SYNC_NEED_UPLOAD,
    ACTION_SETTING_SYNC_RENAME,
    WSMessage,
)

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.setting_sync")

_DELETED = "__deleted__"


def _extract_inner(msg_data: dict) -> dict:
    """Server wraps payloads as {code, status, message, data: {actual fields}}."""
    if isinstance(msg_data, dict) and "data" in msg_data:
        inner = msg_data["data"]
        if isinstance(inner, dict):
            return inner
    return msg_data if isinstance(msg_data, dict) else {}


def _is_config_path(rel: str) -> bool:
    """Check whether a relative path belongs to config/settings scope.

    This matches the Obsidian plugin behaviour: anything inside a dot-prefixed
    directory (e.g. .obsidian, .agents) is treated as a setting file.
    Standard exclusions (.git, .trash) are handled by is_excluded() upstream.
    """
    first = rel.split("/")[0]
    return first.startswith(".")


class SettingSync:
    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.config = engine.config
        self.vault_path = engine.vault_path
        self._sync_complete = False
        self._expected_modify = 0
        self._expected_delete = 0
        self._received_modify = 0
        self._received_delete = 0
        self._got_end = False
        self._pending_last_time = 0
        self._echo_hashes: dict[str, str] = {}

    @property
    def is_sync_complete(self) -> bool:
        return self._sync_complete

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_SETTING_SYNC_MODIFY, self._on_sync_modify)
        ws.on(ACTION_SETTING_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_SETTING_SYNC_RENAME, self._on_sync_rename)
        ws.on(ACTION_SETTING_SYNC_MTIME, self._on_sync_mtime)
        ws.on(ACTION_SETTING_SYNC_NEED_UPLOAD, self._on_sync_need_upload)
        ws.on(ACTION_SETTING_SYNC_END, self._on_sync_end)

    async def request_sync(self) -> None:
        """Send incremental SettingSync request."""
        self._reset_counters()
        last_time = self.engine.state.last_setting_sync_time
        ctx = str(uuid.uuid4())
        settings = self._collect_local_settings()
        msg = WSMessage(ACTION_SETTING_SYNC, {
            "context": ctx,
            "vault": self.config.server.vault,
            "lastTime": last_time,
            "settings": settings,
        })
        log.info("Requesting SettingSync (lastTime=%d, localSettings=%d)", last_time, len(settings))
        await self.engine.ws_client.send(msg)

    async def request_full_sync(self) -> None:
        """Full sync: send all local settings for comparison."""
        self._reset_counters()
        settings = self._collect_local_settings()
        ctx = str(uuid.uuid4())
        msg = WSMessage(ACTION_SETTING_SYNC, {
            "context": ctx,
            "vault": self.config.server.vault,
            "lastTime": 0,
            "settings": settings,
        })
        log.info("Requesting full SettingSync with %d local settings", len(settings))
        await self.engine.ws_client.send(msg)

    async def push_modify(self, rel_path: str, *, force: bool = False) -> None:
        full = self.vault_path / rel_path
        if not full.exists():
            return

        hash_ = file_content_hash_binary(full)
        if not force and self._echo_hashes.get(rel_path) == hash_:
            return

        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Some config files (e.g. binary plugins) may not be text; skip for now.
            log.warning("Skipping non-text setting file: %s", rel_path)
            return
        except Exception:
            log.exception("Failed to read %s", rel_path)
            return

        stat = full.stat()
        msg = WSMessage(ACTION_SETTING_MODIFY, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
            "content": text,
            "contentHash": hash_,
            "ctime": int(stat.st_ctime * 1000),
            "mtime": int(stat.st_mtime * 1000),
        })
        log.info("SettingModify → %s", rel_path)
        await self.engine.ws_client.send(msg)
        self._echo_hashes[rel_path] = hash_

    async def push_delete(self, rel_path: str) -> None:
        if self._echo_hashes.get(rel_path) == _DELETED:
            return
        msg = WSMessage(ACTION_SETTING_DELETE, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
        })
        log.info("SettingDelete → %s", rel_path)
        await self.engine.ws_client.send(msg)
        self._echo_hashes[rel_path] = _DELETED

    async def push_rename(self, new_rel: str, old_rel: str) -> None:
        await self.push_modify(new_rel)
        await self.push_delete(old_rel)

    # ── Server → Client handlers ─────────────────────────────────────

    async def _on_sync_modify(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        content: str = data.get("content", "")
        mtime = data.get("mtime", 0)

        if not rel_path:
            return

        full = self.vault_path / rel_path
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            if mtime:
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
            self._echo_hashes[rel_path] = file_content_hash_binary(full)
            log.info("← SettingSyncModify: %s", rel_path)
        except Exception:
            log.exception("Failed to write setting %s", rel_path)

        self._received_modify += 1
        self._check_all_received()

    async def _on_sync_delete(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            return

        full = self.vault_path / rel_path
        try:
            if full.exists():
                full.unlink()
                log.info("← SettingSyncDelete: %s", rel_path)
                self._try_remove_empty_parent(full)
            self._echo_hashes[rel_path] = _DELETED
        except Exception:
            log.exception("Failed to delete setting %s", rel_path)

        self._received_delete += 1
        self._check_all_received()

    async def _on_sync_rename(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        old_path: str = data.get("oldPath", "")
        new_path: str = data.get("path", "")
        if not old_path or not new_path:
            return

        self._echo_hashes[old_path] = _DELETED
        old_full = self.vault_path / old_path
        new_full = self.vault_path / new_path
        try:
            if old_full.exists():
                new_full.parent.mkdir(parents=True, exist_ok=True)
                old_full.rename(new_full)
                if new_full.exists():
                    self._echo_hashes[new_path] = file_content_hash_binary(new_full)
                log.info("← SettingSyncRename: %s → %s", old_path, new_path)
                self._try_remove_empty_parent(old_full)
        except Exception:
            log.exception("Failed to rename setting %s → %s", old_path, new_path)

    async def _on_sync_mtime(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        mtime = data.get("mtime", 0)
        if not rel_path or not mtime:
            return
        full = self.vault_path / rel_path
        if full.exists():
            try:
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
            except OSError:
                pass

    async def _on_sync_need_upload(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        need_upload = data.get("needUpload", [])
        if not isinstance(need_upload, list) or not need_upload:
            return
        log.info("← SettingSyncNeedUpload: %d files", len(need_upload))
        for item in need_upload:
            rel_path = item.get("path", "") if isinstance(item, dict) else str(item)
            if rel_path:
                await self.push_modify(rel_path)

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        last_time = data.get("lastTime", 0)
        self._expected_modify = data.get("needModifyCount", 0)
        self._expected_delete = data.get("needDeleteCount", 0)
        self._pending_last_time = last_time
        self._got_end = True
        log.info(
            "← SettingSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time,
            self._expected_modify,
            self._expected_delete,
            data.get("needUploadCount", 0),
        )

        total_expected = self._expected_modify + self._expected_delete
        if total_expected == 0:
            self._sync_complete = True
            self._commit_last_time()
        else:
            self._check_all_received()

    # ── Internal helpers ─────────────────────────────────────────────

    def _reset_counters(self) -> None:
        self._sync_complete = False
        self._got_end = False
        self._expected_modify = 0
        self._expected_delete = 0
        self._received_modify = 0
        self._received_delete = 0
        self._pending_last_time = 0

    def _check_all_received(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_modify + self._expected_delete
        total_received = self._received_modify + self._received_delete
        if total_received >= total_expected:
            log.info(
                "SettingSync complete: %d modified, %d deleted",
                self._received_modify,
                self._received_delete,
            )
            self._sync_complete = True
            self._commit_last_time()

    def _commit_last_time(self) -> None:
        if self._pending_last_time:
            log.info("Committing setting lastTime=%d", self._pending_last_time)
            self.engine.state.last_setting_sync_time = self._pending_last_time
            self.engine.state.save()
            self._pending_last_time = 0

    def _try_remove_empty_parent(self, file_path: Path) -> None:
        parent = file_path.parent
        while parent != self.vault_path:
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                else:
                    break
            except OSError:
                break
            parent = parent.parent

    def _collect_local_settings(self) -> list[dict]:
        """Collect all config files under dot-prefixed directories."""
        settings = []
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel):
                continue
            if not _is_config_path(rel):
                continue
            try:
                hash_ = file_content_hash_binary(fp)
            except Exception:
                continue
            stat = fp.stat()
            settings.append({
                "path": rel,
                "pathHash": path_hash(rel),
                "contentHash": hash_,
                "mtime": int(stat.st_mtime * 1000),
                "ctime": int(stat.st_ctime * 1000),
                "size": stat.st_size,
            })
        return settings
