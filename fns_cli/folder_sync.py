"""Folder sync protocol: apply server-pushed folder create/delete/rename events."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .protocol import (
    ACTION_FOLDER_SYNC_DELETE,
    ACTION_FOLDER_SYNC_MODIFY,
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

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_FOLDER_SYNC_MODIFY, self._on_sync_modify)
        ws.on(ACTION_FOLDER_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_FOLDER_SYNC_RENAME, self._on_sync_rename)

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
            return

        if self._is_config_dir(rel_path):
            log.debug("Ignoring FolderSyncModify for config dir: %s", rel_path)
            return

        full = self.vault_path / rel_path
        try:
            full.mkdir(parents=True, exist_ok=True)
            log.info("← FolderSyncModify: %s", rel_path)
        except Exception:
            log.exception("Failed to create folder %s", rel_path)

    async def _on_sync_delete(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            return

        if self._is_config_dir(rel_path):
            log.debug("Ignoring FolderSyncDelete for config dir: %s", rel_path)
            return

        full = self.vault_path / rel_path
        try:
            if full.exists():
                shutil.rmtree(full)
                log.info("← FolderSyncDelete: %s", rel_path)
        except Exception:
            log.exception("Failed to delete folder %s", rel_path)

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
