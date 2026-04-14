"""Note sync protocol: NoteSync incremental pull + NoteModify/NoteDelete push."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .hash_utils import content_hash, path_hash
from .protocol import (
    ACTION_NOTE_DELETE,
    ACTION_NOTE_MODIFY,
    ACTION_NOTE_SYNC,
    ACTION_NOTE_SYNC_DELETE,
    ACTION_NOTE_SYNC_END,
    ACTION_NOTE_SYNC_MODIFY,
    ACTION_NOTE_SYNC_MTIME,
    ACTION_NOTE_SYNC_NEED_PUSH,
    WSMessage,
)

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.note_sync")


def _extract_inner(msg_data: dict) -> dict:
    """Server wraps payloads as {code, status, message, data: {actual fields}}."""
    if isinstance(msg_data, dict) and "data" in msg_data:
        inner = msg_data["data"]
        if isinstance(inner, dict):
            return inner
    return msg_data if isinstance(msg_data, dict) else {}


class NoteSync:
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

    @property
    def is_sync_complete(self) -> bool:
        return self._sync_complete

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_NOTE_SYNC_MODIFY, self._on_sync_modify)
        ws.on(ACTION_NOTE_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_NOTE_SYNC_MTIME, self._on_sync_mtime)
        ws.on(ACTION_NOTE_SYNC_NEED_PUSH, self._on_sync_need_push)
        ws.on(ACTION_NOTE_SYNC_END, self._on_sync_end)

    async def request_sync(self) -> None:
        """Send incremental NoteSync request."""
        self._reset_counters()
        last_time = self.engine.state.last_note_sync_time
        ctx = str(uuid.uuid4())
        msg = WSMessage(ACTION_NOTE_SYNC, {
            "context": ctx,
            "vault": self.config.server.vault,
            "lastTime": last_time,
            "notes": [],
        })
        log.info("Requesting NoteSync (lastTime=%d)", last_time)
        await self.engine.ws_client.send(msg)

    async def request_full_sync(self) -> None:
        """Full sync: send all local notes for comparison."""
        self._reset_counters()
        notes = self._collect_local_notes()
        ctx = str(uuid.uuid4())
        msg = WSMessage(ACTION_NOTE_SYNC, {
            "context": ctx,
            "vault": self.config.server.vault,
            "lastTime": 0,
            "notes": notes,
        })
        log.info("Requesting full NoteSync with %d local notes", len(notes))
        await self.engine.ws_client.send(msg)

    async def push_modify(self, rel_path: str) -> None:
        full = self.vault_path / rel_path
        if not full.exists():
            return
        try:
            text = full.read_text(encoding="utf-8")
        except Exception:
            log.exception("Failed to read %s", rel_path)
            return

        stat = full.stat()
        msg = WSMessage(ACTION_NOTE_MODIFY, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
            "content": text,
            "contentHash": content_hash(text),
            "ctime": int(stat.st_ctime * 1000),
            "mtime": int(stat.st_mtime * 1000),
        })
        log.info("NoteModify → %s", rel_path)
        await self.engine.ws_client.send(msg)

    async def push_delete(self, rel_path: str) -> None:
        msg = WSMessage(ACTION_NOTE_DELETE, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
        })
        log.info("NoteDelete → %s", rel_path)
        await self.engine.ws_client.send(msg)

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
        self.engine.ignore_file(rel_path)
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            if mtime:
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
            log.info("← NoteSyncModify: %s", rel_path)
        except Exception:
            log.exception("Failed to write %s", rel_path)
        finally:
            await asyncio.sleep(0.6)
            self.engine.unignore_file(rel_path)

        self._received_modify += 1
        self._check_all_received()

    async def _on_sync_delete(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            return
        full = self.vault_path / rel_path
        self.engine.ignore_file(rel_path)
        try:
            if full.exists():
                full.unlink()
                log.info("← NoteSyncDelete: %s", rel_path)
                self._try_remove_empty_parent(full)
        except Exception:
            log.exception("Failed to delete %s", rel_path)
        finally:
            await asyncio.sleep(0.6)
            self.engine.unignore_file(rel_path)

        self._received_delete += 1
        self._check_all_received()

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

    async def _on_sync_need_push(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        if not rel_path:
            return
        log.info("← NoteSyncNeedPush: %s", rel_path)
        await self.push_modify(rel_path)

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        last_time = data.get("lastTime", 0)
        self._expected_modify = data.get("needModifyCount", 0)
        self._expected_delete = data.get("needDeleteCount", 0)

        if last_time:
            self.engine.state.last_note_sync_time = last_time
            self.engine.state.save()

        self._got_end = True
        log.info(
            "← NoteSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time,
            self._expected_modify,
            self._expected_delete,
            data.get("needUploadCount", 0),
        )

        total_expected = self._expected_modify + self._expected_delete
        if total_expected == 0:
            self._sync_complete = True
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

    def _check_all_received(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_modify + self._expected_delete
        total_received = self._received_modify + self._received_delete
        if total_received >= total_expected:
            log.info(
                "NoteSync complete: %d modified, %d deleted",
                self._received_modify,
                self._received_delete,
            )
            self._sync_complete = True

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

    def _collect_local_notes(self) -> list[dict]:
        notes = []
        for md in self.vault_path.rglob("*.md"):
            rel = md.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            stat = md.stat()
            notes.append({
                "path": rel,
                "pathHash": path_hash(rel),
                "contentHash": content_hash(text),
                "mtime": int(stat.st_mtime * 1000),
            })
        return notes
