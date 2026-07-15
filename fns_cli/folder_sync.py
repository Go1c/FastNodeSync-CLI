"""Folder sync protocol: apply server-pushed folder create/delete/rename events."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .protocol import (
    ACTION_FOLDER_SYNC_DELETE,
    ACTION_FOLDER_SYNC_END,
    ACTION_FOLDER_SYNC_MODIFY,
    ACTION_FOLDER_SYNC_PAGE,
    ACTION_FOLDER_SYNC_PAGE_ACK,
    ACTION_FOLDER_SYNC_RENAME,
    WSMessage,
)

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.folder_sync")


def _extract_inner(msg_data: dict) -> dict:
    if isinstance(msg_data, dict) and "data" in msg_data:
        inner = msg_data["data"]
        if isinstance(inner, dict):
            return inner
    return msg_data if isinstance(msg_data, dict) else {}


class FolderSync:
    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.vault_path = engine.vault_path
        self._sync_context: str = ""
        self._sync_vault: str = ""
        self._expected_modify: int = 0
        self._expected_delete: int = 0
        self._received_modify: int = 0
        self._received_delete: int = 0
        self._got_end: bool = False
        self._sync_complete: bool = False

    @property
    def is_sync_complete(self) -> bool:
        return self._sync_complete

    def _reset_counters(self) -> None:
        self._sync_complete = False
        self._got_end = False
        self._expected_modify = 0
        self._expected_delete = 0
        self._received_modify = 0
        self._received_delete = 0
        self._sync_context = ""
        self._sync_vault = ""

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_FOLDER_SYNC_MODIFY, self._on_sync_modify)
        ws.on(ACTION_FOLDER_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_FOLDER_SYNC_RENAME, self._on_sync_rename)
        ws.on(ACTION_FOLDER_SYNC_END, self._on_sync_end)
        ws.on(ACTION_FOLDER_SYNC_PAGE, self._on_sync_page)

    def _is_config_dir(self, rel_path: str) -> bool:
        """Check if a path is in a config directory managed by SettingSync."""
        first = rel_path.split("/")[0]
        if not first.startswith("."):
            return False
        # Use the same logic as SyncEngine._is_config()
        config = self.engine.config.sync
        if first in config.config_sync_dirs:
            return True
        return config.sync_config

    async def _on_sync_modify(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            self._received_modify += 1
            self._check_all_received()
            return

        if self._is_config_dir(rel_path):
            log.debug("Ignoring FolderSyncModify for config dir: %s", rel_path)
            self._received_modify += 1
            self._check_all_received()
            return

        full = self.vault_path / rel_path
        try:
            full.mkdir(parents=True, exist_ok=True)
            log.info("← FolderSyncModify: %s", rel_path)
        except Exception:
            log.exception("Failed to create folder %s", rel_path)

        self._received_modify += 1
        self._check_all_received()

    async def _on_sync_delete(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            self._received_delete += 1
            self._check_all_received()
            return

        if self._is_config_dir(rel_path):
            log.debug("Ignoring FolderSyncDelete for config dir: %s", rel_path)
            self._received_delete += 1
            self._check_all_received()
            return

        full = self.vault_path / rel_path
        try:
            if full.exists():
                shutil.rmtree(full)
                log.info("← FolderSyncDelete: %s", rel_path)
        except Exception:
            log.exception("Failed to delete folder %s", rel_path)

        self._received_delete += 1
        self._check_all_received()

    async def _on_sync_rename(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        old_path: str = data.get("oldPath", "")
        new_path: str = data.get("path", "")
        if not old_path or not new_path:
            return

        if self._is_config_dir(old_path) or self._is_config_dir(new_path):
            log.debug("Ignoring FolderSyncRename for config dir: %s → %s", old_path, new_path)
            return

        old_full = self.vault_path / old_path
        new_full = self.vault_path / new_path
        try:
            new_full.parent.mkdir(parents=True, exist_ok=True)
            if old_full.exists():
                old_full.rename(new_full)
            else:
                new_full.mkdir(parents=True, exist_ok=True)
            log.info("← FolderSyncRename: %s → %s", old_path, new_path)
        except Exception:
            log.exception("Failed to rename folder %s → %s", old_path, new_path)

    async def _send_page_ack(self, context: str, page_index: int, vault: str) -> None:
        msg = WSMessage(
            ACTION_FOLDER_SYNC_PAGE_ACK,
            {
                "context": context,
                "pageIndex": page_index,
                "vault": vault,
            },
        )
        await self.engine.ws_client.send(msg)

    async def _on_sync_page(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        page_index = data.get("pageIndex", msg.data.get("pageIndex", 0)) if isinstance(msg.data, dict) else data.get("pageIndex", 0)
        is_last = data.get("isLast", msg.data.get("isLast", False)) if isinstance(msg.data, dict) else data.get("isLast", False)
        page_index = int(page_index or 0)
        is_last = bool(is_last)
        log.debug("← FolderSyncPage (pageIndex=%d, isLast=%s)", page_index, is_last)

        if not is_last and page_index >= 0:
            await self._send_page_ack(self._sync_context, page_index, self._sync_vault)

        self._check_all_received()

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        if isinstance(msg.data, dict):
            self._sync_context = (msg.data.get("context") or (data.get("context") if isinstance(data, dict) else "") or "")
            self._sync_vault = (msg.data.get("vault") or (data.get("vault") if isinstance(data, dict) else "") or "")
        elif isinstance(data, dict):
            self._sync_context = data.get("context") or ""
            self._sync_vault = data.get("vault") or ""

        self._expected_modify = data.get("needModifyCount", 0)
        self._expected_delete = data.get("needDeleteCount", 0)
        self._got_end = True
        log.info(
            "← FolderSyncEnd (needModify=%d, needDelete=%d)",
            self._expected_modify,
            self._expected_delete,
        )

        total_expected = self._expected_modify + self._expected_delete
        if total_expected == 0:
            self._sync_complete = True
        else:
            await self._send_page_ack(self._sync_context, -1, self._sync_vault)
            self._check_all_received()

    def _check_all_received(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_modify + self._expected_delete
        total_received = self._received_modify + self._received_delete
        if total_received >= total_expected:
            log.info("FolderSync complete: %d modified, %d deleted", self._received_modify, self._received_delete)
            self._sync_complete = True
