"""File (attachment) sync: FileSync protocol, chunked upload/download."""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
    ACTION_FILE_UPLOAD_ACK,
    ACTION_FILE_UPLOAD_CHECK,
    WSMessage,
    build_binary_chunk,
)

if TYPE_CHECKING:
    from .sync_engine import SyncEngine

log = logging.getLogger("fns_cli.file_sync")

# Sentinel stored in _echo_hashes to mark a just-received delete.
_DELETED = "__deleted__"


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
        self._pending_download_paths: set[str] = set()
        self._expected_modify = 0
        self._expected_delete = 0
        self._received_modify = 0
        self._received_delete = 0
        self._got_end = False
        self._pending_last_time = 0
        self._last_sync_activity_monotonic = time.monotonic()
        self._upload_tasks: set[asyncio.Task] = set()
        upload_concurrency = getattr(self.config.sync, "upload_concurrency", 2)
        if not isinstance(upload_concurrency, int) or upload_concurrency < 1:
            upload_concurrency = 2
        self._upload_worker_count = upload_concurrency
        self._upload_workers: set[asyncio.Task] = set()
        self._upload_queue: asyncio.Queue = asyncio.Queue()
        # See NoteSync._echo_hashes — same semantics here. Updated on both
        # inbound (server write / chunk finalize / rename / delete) and
        # outbound (push_upload / push_delete) so the cache tracks the most
        # recently known synced state, not just what the server pushed.
        self._echo_hashes: dict[str, str] = {}

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
        ws.on(ACTION_FILE_UPLOAD_ACK, self._on_upload_ack)
        ws.on(ACTION_FILE_SYNC_END, self._on_sync_end)
        ws.on_binary(self._on_binary_chunk)

    def _reset_counters(self) -> None:
        self._sync_complete = False
        self._got_end = False
        self._download_sessions.clear()
        self._pending_download_paths.clear()
        self._expected_modify = 0
        self._expected_delete = 0
        self._received_modify = 0
        self._received_delete = 0
        self._pending_last_time = 0
        self._last_sync_activity_monotonic = time.monotonic()

    def _mark_sync_activity(self) -> None:
        self._last_sync_activity_monotonic = time.monotonic()

    def is_stalled(self, stale_seconds: float) -> bool:
        if self._sync_complete or not self._got_end:
            return False
        if self._pending_download_paths or self._download_sessions:
            return False
        total_expected = self._expected_modify + self._expected_delete
        total_received = self._received_modify + self._received_delete
        if total_received >= total_expected:
            return False
        return (time.monotonic() - self._last_sync_activity_monotonic) >= stale_seconds

    async def request_sync(self) -> None:
        self._reset_counters()
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

        hash_ = file_content_hash_binary(full)
        if self._echo_hashes.get(rel_path) == hash_:
            return

        stat = full.stat()
        msg = WSMessage(ACTION_FILE_UPLOAD_CHECK, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
            "contentHash": hash_,
            "size": stat.st_size,
            "ctime": int(stat.st_ctime * 1000),
            "mtime": int(stat.st_mtime * 1000),
        })
        log.info("FileUploadCheck → %s (%d bytes)", rel_path, stat.st_size)
        await self.engine.ws_client.send(msg)
        # Record the outbound hash so a later revert-to-previous-content
        # still differs from the cache and triggers a real upload.
        self._echo_hashes[rel_path] = hash_

    async def push_delete(self, rel_path: str) -> None:
        if self._echo_hashes.get(rel_path) == _DELETED:
            return
        msg = WSMessage(ACTION_FILE_DELETE, {
            "vault": self.config.server.vault,
            "path": rel_path,
            "pathHash": path_hash(rel_path),
        })
        log.info("FileDelete → %s", rel_path)
        await self.engine.ws_client.send(msg)
        self._echo_hashes[rel_path] = _DELETED

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

        await self._ensure_upload_workers()
        completion = asyncio.get_running_loop().create_future()
        await self._upload_queue.put((session_id, chunk_size, rel_path, full, completion))
        task = asyncio.create_task(self._await_upload_completion(completion))
        self._upload_tasks.add(task)
        task.add_done_callback(self._upload_tasks.discard)

    async def _ensure_upload_workers(self) -> None:
        while len(self._upload_workers) < self._upload_worker_count:
            task = asyncio.create_task(self._upload_queue_worker())
            self._upload_workers.add(task)
            task.add_done_callback(self._upload_workers.discard)

    async def _await_upload_completion(self, completion: asyncio.Future) -> None:
        await completion

    async def _upload_queue_worker(self) -> None:
        while True:
            session_id, chunk_size, rel_path, full, completion = await self._upload_queue.get()
            try:
                await self._upload_session_worker(session_id, chunk_size, rel_path, full)
            except Exception as exc:
                if not completion.done():
                    completion.set_exception(exc)
            else:
                if not completion.done():
                    completion.set_result(None)
            finally:
                self._upload_queue.task_done()

    async def _upload_session_worker(
        self,
        session_id: str,
        chunk_size: int,
        rel_path: str,
        full: Path,
    ) -> None:
        log.info(
            "Uploading %s (sessionId=%s, chunkSize=%d)",
            rel_path,
            session_id[:8],
            chunk_size,
        )

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
            await asyncio.sleep(0)
        if total == 0:
            chunk = build_binary_chunk(session_id, 0, b"")
            await self.engine.ws_client.send_bytes(chunk)
        log.info("Upload complete: %s (%d chunks)", rel_path, idx)

    async def _on_upload_ack(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path = data.get("path", "")
        if rel_path:
            log.debug("← FileUploadAck: %s", rel_path)

    async def _on_sync_update(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
        rel_path: str = data.get("path", "")
        content = data.get("content")
        mtime = data.get("mtime", 0)

        if not rel_path:
            return

        if content is None:
            # Attachment files: server sends metadata only, we must request
            # a chunked download via FileChunkDownload.
            self._pending_download_paths.add(rel_path)
            log.info("← FileSyncUpdate (requesting chunk download): %s", rel_path)
            await self._request_chunk_download(rel_path, data)
            self._received_modify += 1
            self._check_complete()
            return

        full = self.vault_path / rel_path
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                full.write_text(content, encoding="utf-8")
            elif isinstance(content, bytes):
                full.write_bytes(content)
            else:
                log.warning("Unexpected content type for %s: %s", rel_path, type(content))
            if mtime and full.exists():
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
            # Record echo hash after the write is durable on disk.
            if full.exists():
                self._echo_hashes[rel_path] = file_content_hash_binary(full)
            log.info("← FileSyncUpdate: %s", rel_path)
        except Exception:
            log.exception("Failed to write file %s", rel_path)

        self._received_modify += 1
        self._check_complete()

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
        self._mark_sync_activity()
        rel_path: str = data.get("path", "")
        if not rel_path:
            return

        self._echo_hashes[rel_path] = _DELETED

        full = self.vault_path / rel_path
        try:
            if full.exists():
                full.unlink()
                log.info("← FileSyncDelete: %s", rel_path)
                self._try_remove_empty_parent(full)
        except Exception:
            log.exception("Failed to delete file %s", rel_path)

        self._received_delete += 1
        self._check_complete()

    async def _on_sync_rename(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
        old_path: str = data.get("oldPath", "")
        new_path: str = data.get("path", "")
        if not old_path or not new_path:
            return

        # The rename arrives as a single server message but the watcher will
        # observe it as delete(old) + create(new). Prime the echo cache for
        # both paths.
        self._echo_hashes[old_path] = _DELETED
        old_full = self.vault_path / old_path
        new_full = self.vault_path / new_path
        try:
            if old_full.exists():
                new_full.parent.mkdir(parents=True, exist_ok=True)
                old_full.rename(new_full)
                if new_full.exists():
                    self._echo_hashes[new_path] = file_content_hash_binary(new_full)
                log.info("← FileSyncRename: %s → %s", old_path, new_path)
        except Exception:
            log.exception("Failed to rename file %s → %s", old_path, new_path)

    async def _on_sync_mtime(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
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
        self._mark_sync_activity()
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
        if total_chunks <= 0:
            await self._finalize_empty_download(rel_path)
            return
        self._download_sessions[session_id] = _DownloadSession(
            path=rel_path, size=size, total_chunks=total_chunks, chunk_size=chunk_size,
        )
        self._pending_download_paths.discard(rel_path)

    async def _on_binary_chunk(self, session_id: str, chunk_index: int, data: bytes) -> None:
        session = self._download_sessions.get(session_id)
        if not session:
            return

        session.chunks[chunk_index] = data

        if session.complete:
            await self._finalize_download(session_id, session)

    async def _finalize_download(self, session_id: str, session: _DownloadSession) -> None:
        self._mark_sync_activity()
        rel_path = session.path
        full = self.vault_path / rel_path
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "wb") as f:
                for i in range(session.total_chunks):
                    f.write(session.chunks.get(i, b""))
            if full.exists():
                self._echo_hashes[rel_path] = file_content_hash_binary(full)
            log.info("← Chunked download complete: %s", rel_path)
        except Exception:
            log.exception("Failed to write downloaded file %s", rel_path)
        finally:
            self._download_sessions.pop(session_id, None)
            self._pending_download_paths.discard(rel_path)
            self._check_complete()

    async def _finalize_empty_download(self, rel_path: str) -> None:
        self._mark_sync_activity()
        full = self.vault_path / rel_path
        try:
            if full.exists() and full.is_dir():
                log.warning(
                    "Skipping zero-chunk download for directory path: %s",
                    rel_path,
                )
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(b"")
                self._echo_hashes[rel_path] = file_content_hash_binary(full)
                log.info("← Zero-chunk download complete: %s", rel_path)
        except Exception:
            log.exception("Failed to finalize zero-chunk download %s", rel_path)
        finally:
            self._pending_download_paths.discard(rel_path)
            self._check_complete()

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
        last_time = data.get("lastTime", 0)
        self._pending_last_time = last_time
        self._expected_modify = data.get("needModifyCount", 0)
        self._expected_delete = data.get("needDeleteCount", 0)
        need_upload = data.get("needUploadCount", 0)

        self._got_end = True
        log.info(
            "← FileSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time, self._expected_modify, self._expected_delete, need_upload,
        )

        self._check_complete()

    def _check_complete(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_modify + self._expected_delete
        total_received = self._received_modify + self._received_delete
        if total_received >= total_expected and not self._pending_download_paths and not self._download_sessions:
            log.info(
                "FileSync complete: %d modified, %d deleted",
                self._received_modify, self._received_delete,
            )
            self._sync_complete = True
            self._commit_last_time()
        elif total_received >= total_expected and (self._pending_download_paths or self._download_sessions):
            log.info(
                "FileSync detail messages complete, waiting for %d pending downloads and %d active sessions",
                len(self._pending_download_paths),
                len(self._download_sessions),
            )

    def _commit_last_time(self) -> None:
        if self._pending_last_time:
            log.info("Committing file lastTime=%d", self._pending_last_time)
            self.engine.state.last_file_sync_time = self._pending_last_time
            self.engine.state.save()
            self._pending_last_time = 0

    def _collect_local_files(self) -> list[dict]:
        """Collect non-note, non-excluded local files with hashes for FileSync."""
        files = []
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel) or rel.endswith(".md"):
                continue
            first = rel.split("/")[0]
            if first.startswith(".") and not self.config.sync.sync_config:
                continue
            if not first.startswith(".") and not self.config.sync.sync_files:
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
        while parent != self.vault_path:
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                else:
                    break
            except OSError:
                break
            parent = parent.parent
