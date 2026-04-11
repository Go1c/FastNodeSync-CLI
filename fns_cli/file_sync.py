"""File (attachment) sync: FileSync protocol, chunked upload/download."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .hash_utils import file_content_hash_binary, path_hash
from .protocol import (
    ACTION_FILE_CHUNK_DOWNLOAD,
    ACTION_FILE_DELETE,
    ACTION_FILE_SYNC,
    ACTION_FILE_SYNC_CHUNK_DOWNLOAD,
    ACTION_FILE_SYNC_DELETE,
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_MTIME,
    ACTION_FILE_SYNC_RENAME,
    ACTION_FILE_SYNC_UPDATE,
    ACTION_FILE_UPLOAD,
    ACTION_FILE_UPLOAD_CHECK,
    WSMessage,
    build_binary_chunk,
)

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.file_sync")


def _extract_inner(msg_data: dict) -> dict:
    """Server wraps payloads as {code, status, message, data: {actual fields}}."""
    if isinstance(msg_data, dict) and "data" in msg_data:
        inner = msg_data["data"]
        if isinstance(inner, dict):
            return inner
    return msg_data if isinstance(msg_data, dict) else {}


class _DownloadSession:
    __slots__ = ("path", "size", "total_chunks", "chunks", "chunk_size")

    def __init__(self, path: str, size: int, total_chunks: int, chunk_size: int):
        self.path = path
        self.size = size
        self.total_chunks = total_chunks
        self.chunk_size = chunk_size
        self.chunks: dict[int, bytes] = {}

    @property
    def complete(self) -> bool:
        return len(self.chunks) >= self.total_chunks


class FileSync:
    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.config = engine.config
        self.vault_path = engine.vault_path
        self._sync_complete = False
        self._download_sessions: dict[str, _DownloadSession] = {}

    @property
    def is_sync_complete(self) -> bool:
        return self._sync_complete

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_FILE_SYNC_UPDATE, self._on_sync_update)
        ws.on(ACTION_FILE_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_FILE_SYNC_RENAME, self._on_sync_rename)
        ws.on(ACTION_FILE_SYNC_MTIME, self._on_sync_mtime)
        ws.on(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, self._on_chunk_download_start)
        ws.on(ACTION_FILE_UPLOAD, self._on_upload_session)
        ws.on(ACTION_FILE_SYNC_END, self._on_sync_end)
        ws.on_binary(self._on_binary_chunk)

    async def request_sync(self) -> None:
        self._sync_complete = False
        last_time = self.engine.state.last_file_sync_time
        ctx = str(uuid.uuid4())
        files = self._collect_local_files()
        msg = WSMessage(ACTION_FILE_SYNC, {
            "context": ctx,
            "vault": self.config.server.vault,
            "lastTime": last_time,
            "files": files,
        })
        log.info("Requesting FileSync (lastTime=%d, localFiles=%d)", last_time, len(files))
        await self.engine.ws_client.send(msg)

    async def push_upload(self, rel_path: str) -> None:
        full = self.vault_path / rel_path
        if not full.exists():
            return

        stat = full.stat()
        msg = WSMessage(ACTION_FILE_UPLOAD_CHECK, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
            "contentHash": file_content_hash_binary(full),
            "size": stat.st_size,
            "ctime": int(stat.st_ctime * 1000),
            "mtime": int(stat.st_mtime * 1000),
        })
        log.info("FileUploadCheck → %s (%d bytes)", rel_path, stat.st_size)
        await self.engine.ws_client.send(msg)

    async def push_delete(self, rel_path: str) -> None:
        msg = WSMessage(ACTION_FILE_DELETE, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
        })
        log.info("FileDelete → %s", rel_path)
        await self.engine.ws_client.send(msg)

    # ── Server → Client handlers ─────────────────────────────────────

    async def _on_upload_session(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        session_id: str = data.get("sessionId", "")
        chunk_size: int = data.get("chunkSize", self.config.sync.file_chunk_size)
        rel_path: str = data.get("path", "")

        if not session_id or not rel_path:
            return

        full = self.vault_path / rel_path
        if not full.exists():
            log.warning("Upload requested but file missing: %s", rel_path)
            return

        log.info("Uploading %s (sessionId=%s, chunkSize=%d)", rel_path, session_id[:8], chunk_size)

        try:
            file_data = full.read_bytes()
            total = len(file_data)
            idx = 0
            offset = 0
            while offset < total:
                end = min(offset + chunk_size, total)
                chunk = build_binary_chunk(session_id, idx, file_data[offset:end])
                await self.engine.ws_client.send_bytes(chunk)
                offset = end
                idx += 1
            if total == 0:
                chunk = build_binary_chunk(session_id, 0, b"")
                await self.engine.ws_client.send_bytes(chunk)
            log.info("Upload complete: %s (%d chunks)", rel_path, idx)
        except Exception:
            log.exception("Upload failed for %s", rel_path)

    async def _on_sync_update(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path: str = data.get("path", "")
        content = data.get("content")
        mtime = data.get("mtime", 0)

        if not rel_path:
            return

        if content is None:
            # Attachment files: server sends metadata only, we must request
            # a chunked download via FileChunkDownload.
            log.info("← FileSyncUpdate (requesting chunk download): %s", rel_path)
            await self._request_chunk_download(rel_path, data)
            return

        full = self.vault_path / rel_path
        self.engine.ignore_file(rel_path)
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                full.write_text(content, encoding="utf-8")
            elif isinstance(content, bytes):
                full.write_bytes(content)
            else:
                log.warning("Unexpected content type for %s: %s", rel_path, type(content))
                return
            if mtime and full.exists():
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
            log.info("← FileSyncUpdate: %s", rel_path)
        except Exception:
            log.exception("Failed to write file %s", rel_path)
        finally:
            self.engine.unignore_file(rel_path)

    async def _request_chunk_download(self, rel_path: str, data: dict) -> None:
        """Send FileChunkDownload request for a file that needs chunked transfer."""
        msg = WSMessage(ACTION_FILE_CHUNK_DOWNLOAD, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": data.get("pathHash", path_hash(rel_path)),
        })
        await self.engine.ws_client.send(msg)

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
                log.info("← FileSyncDelete: %s", rel_path)
                self._try_remove_empty_parent(full)
        except Exception:
            log.exception("Failed to delete file %s", rel_path)
        finally:
            self.engine.unignore_file(rel_path)

    async def _on_sync_rename(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        old_path: str = data.get("oldPath", "")
        new_path: str = data.get("path", "")
        if not old_path or not new_path:
            return

        old_full = self.vault_path / old_path
        new_full = self.vault_path / new_path
        self.engine.ignore_file(old_path)
        self.engine.ignore_file(new_path)
        try:
            if old_full.exists():
                new_full.parent.mkdir(parents=True, exist_ok=True)
                old_full.rename(new_full)
                log.info("← FileSyncRename: %s → %s", old_path, new_path)
        except Exception:
            log.exception("Failed to rename file %s → %s", old_path, new_path)
        finally:
            self.engine.unignore_file(old_path)
            self.engine.unignore_file(new_path)

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

    async def _on_chunk_download_start(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        session_id: str = data.get("sessionId", "")
        rel_path: str = data.get("path", "")
        size: int = data.get("size", 0)
        total_chunks: int = data.get("totalChunks", 1)
        chunk_size: int = data.get("chunkSize", self.config.sync.file_chunk_size)

        if not session_id or not rel_path:
            return

        log.info(
            "← FileSyncChunkDownload start: %s (%d bytes, %d chunks)",
            rel_path, size, total_chunks,
        )
        self._download_sessions[session_id] = _DownloadSession(
            path=rel_path, size=size, total_chunks=total_chunks, chunk_size=chunk_size,
        )

    async def _on_binary_chunk(self, session_id: str, chunk_index: int, data: bytes) -> None:
        session = self._download_sessions.get(session_id)
        if not session:
            return

        session.chunks[chunk_index] = data

        if session.complete:
            await self._finalize_download(session_id, session)

    async def _finalize_download(self, session_id: str, session: _DownloadSession) -> None:
        rel_path = session.path
        full = self.vault_path / rel_path
        self.engine.ignore_file(rel_path)
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "wb") as f:
                for i in range(session.total_chunks):
                    f.write(session.chunks.get(i, b""))
            log.info("← Chunked download complete: %s", rel_path)
        except Exception:
            log.exception("Failed to write downloaded file %s", rel_path)
        finally:
            self.engine.unignore_file(rel_path)
            self._download_sessions.pop(session_id, None)

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        last_time = data.get("lastTime", 0)
        if last_time:
            self.engine.state.last_file_sync_time = last_time
            self.engine.state.save()

        need_modify = data.get("needModifyCount", 0)
        need_delete = data.get("needDeleteCount", 0)
        need_upload = data.get("needUploadCount", 0)

        log.info(
            "← FileSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time, need_modify, need_delete, need_upload,
        )

        # Mark sync complete — individual messages arrive after this.
        # The sync_engine wait loop will keep running until this flag is set.
        self._sync_complete = True

    def _collect_local_files(self) -> list[dict]:
        """Collect non-note, non-excluded local files with hashes for FileSync."""
        files = []
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel) or rel.endswith(".md"):
                continue
            if rel.startswith(".obsidian/") and not self.config.sync.sync_config:
                continue
            if not rel.startswith(".obsidian/") and not self.config.sync.sync_files:
                continue
            try:
                stat = fp.stat()
                files.append({
                    "path": rel,
                    "pathHash": path_hash(rel),
                    "contentHash": file_content_hash_binary(fp),
                    "mtime": int(stat.st_mtime * 1000),
                    "ctime": int(stat.st_ctime * 1000),
                    "size": stat.st_size,
                })
            except Exception:
                log.debug("Failed to hash file %s, skipping", rel)
        return files

    def _try_remove_empty_parent(self, file_path: Path) -> None:
        parent = file_path.parent
        try:
            if parent != self.vault_path and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
