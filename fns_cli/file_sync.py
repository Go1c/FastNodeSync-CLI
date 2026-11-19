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
from .pathguard import is_safe_rel_path
from .protocol import (
    ACTION_FILE_CHUNK_DOWNLOAD,
    ACTION_FILE_DELETE,
    ACTION_FILE_SYNC,
    ACTION_FILE_SYNC_CHUNK_DOWNLOAD,
    ACTION_FILE_SYNC_DELETE,
    ACTION_FILE_SYNC_END,
    ACTION_FILE_SYNC_MTIME,
    ACTION_FILE_SYNC_PAGE,
    ACTION_FILE_SYNC_PAGE_ACK,
    ACTION_FILE_SYNC_RENAME,
    ACTION_FILE_SYNC_UPDATE,
    ACTION_FILE_UPLOAD,
    ACTION_FILE_UPLOAD_ACK,
    ACTION_FILE_UPLOAD_CHECK,
    WSMessage,
    build_binary_chunk,
)
from .state import SyncState, baseline_matches, commit_round_baseline

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
        self._expected_upload = 0
        self._expected_mtime = 0
        self._received_modify = 0
        self._received_delete = 0
        self._received_upload = 0
        self._received_mtime = 0
        self._got_end = False
        self._pending_last_time = 0
        self._sync_context: str = ""
        self._sync_vault: str = ""
        self._last_sync_activity_monotonic = time.monotonic()
        self._upload_tasks: set[asyncio.Task] = set()
        upload_concurrency = getattr(self.config.sync, "upload_concurrency", 2)
        if not isinstance(upload_concurrency, int) or upload_concurrency < 1:
            upload_concurrency = 2
        self._upload_worker_count = upload_concurrency
        self._upload_workers: set[asyncio.Task] = set()
        self._upload_queue: asyncio.Queue | None = None
        self._last_upload_progress_monotonic = time.monotonic()
        # See NoteSync._echo_hashes — same semantics here. Updated on both
        # inbound (server write / chunk finalize / rename / delete) and
        # outbound (push_upload / push_delete) so the cache tracks the most
        # recently known synced state, not just what the server pushed.
        self._echo_hashes: dict[str, str] = {}
        # Baseline bookkeeping for the current round (see _commit_baseline):
        # entries as reported, uploads still awaiting an ack (path → the entry
        # that was sent — the ack must record that hash, not the current one),
        # upload directives the server actually delivered, and the count it
        # announced in FileSyncEnd.
        self._reported: dict[str, dict] = {}
        self._pending_uploads: dict[str, dict] = {}
        self._upload_directives: set[str] = set()
        self._baseline_committed = False

    @property
    def is_sync_complete(self) -> bool:
        return self._sync_complete

    @property
    def expected_detail_count(self) -> int:
        return self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete

    @property
    def received_detail_count(self) -> int:
        return self._received_upload + self._received_modify + self._received_mtime + self._received_delete

    def register_handlers(self) -> None:
        ws = self.engine.ws_client
        ws.on(ACTION_FILE_SYNC_UPDATE, self._on_sync_update)
        ws.on(ACTION_FILE_SYNC_DELETE, self._on_sync_delete)
        ws.on(ACTION_FILE_SYNC_RENAME, self._on_sync_rename)
        ws.on(ACTION_FILE_SYNC_MTIME, self._on_sync_mtime)
        ws.on(ACTION_FILE_SYNC_CHUNK_DOWNLOAD, self._on_chunk_download_start)
        ws.on(ACTION_FILE_UPLOAD, self._on_upload_session)
        ws.on(ACTION_FILE_UPLOAD_ACK, self._on_upload_ack)
        ws.on(ACTION_FILE_SYNC_PAGE, self._on_sync_page)
        ws.on(ACTION_FILE_SYNC_END, self._on_sync_end)
        ws.on_binary(self._on_binary_chunk)

    def _reset_counters(self) -> None:
        self._sync_complete = False
        self._got_end = False
        self._download_sessions.clear()
        self._pending_download_paths.clear()
        self._expected_modify = 0
        self._expected_delete = 0
        self._expected_upload = 0
        self._expected_mtime = 0
        self._received_modify = 0
        self._received_delete = 0
        self._received_upload = 0
        self._received_mtime = 0
        self._pending_last_time = 0
        self._sync_context = ""
        self._sync_vault = ""
        self._last_sync_activity_monotonic = time.monotonic()
        self._reported = {}
        self._pending_uploads = {}
        self._upload_directives = set()
        self._baseline_committed = False

    def _mark_sync_activity(self) -> None:
        self._last_sync_activity_monotonic = time.monotonic()

    def is_stalled(self, stale_seconds: float) -> bool:
        if self._sync_complete or not self._got_end:
            return False
        if self._pending_download_paths or self._download_sessions:
            return False
        total_expected = self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete
        total_received = self._received_upload + self._received_modify + self._received_mtime + self._received_delete
        if total_received >= total_expected:
            return False
        return (time.monotonic() - self._last_sync_activity_monotonic) >= stale_seconds

    async def request_sync(self) -> None:
        self._reset_counters()
        last_time = self.engine.state.last_file_sync_time
        ctx = str(uuid.uuid4())
        files = self._collect_local_files()
        self._reported = {entry["path"]: entry for entry in files}
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
        self._pending_uploads[rel_path] = {
            "hash": hash_,
            "mtime": int(stat.st_mtime * 1000),
            "size": stat.st_size,
        }
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
        self._mark_sync_activity()
        session_id: str = data.get("sessionId", "")
        chunk_size: int = data.get("chunkSize", self.config.sync.file_chunk_size)
        rel_path: str = data.get("path", "")

        try:
            if not session_id or not rel_path:
                return
            if not is_safe_rel_path(rel_path):
                log.warning("Ignoring upload request for unsafe path: %s", rel_path)
                return

            # The server named this path in an upload directive: it does not
            # consider the reported content to be in sync.
            self._upload_directives.add(rel_path)

            full = self.vault_path / rel_path
            if not full.exists():
                log.warning("Upload requested but file missing: %s", rel_path)
                return

            # Track the upload until the server acknowledges it: the round may
            # finish before the upload does, and the path must not be committed
            # to the baseline while the content is not known to be on the
            # server. A watcher-driven FileUploadCheck already recorded the
            # hash it sent, and the server's upload session carries that same
            # hash; otherwise use the hash from the FileSync report, which is
            # what this session was created for.
            if rel_path not in self._pending_uploads:
                reported = self._reported.get(rel_path)
                if reported is not None:
                    self._pending_uploads[rel_path] = {
                        "hash": reported["contentHash"],
                        "mtime": reported["mtime"],
                        "size": reported["size"],
                    }
                else:
                    try:
                        stat = full.stat()
                        self._pending_uploads[rel_path] = {
                            "hash": file_content_hash_binary(full),
                            "mtime": int(stat.st_mtime * 1000),
                            "size": stat.st_size,
                        }
                    except OSError:
                        log.debug("Cannot stat %s before upload; baseline deferred", rel_path)

            await self._ensure_upload_workers()
            completion = asyncio.get_running_loop().create_future()
            await self._ensure_upload_queue().put(
                (session_id, chunk_size, rel_path, full, completion)
            )
            self._last_upload_progress_monotonic = time.monotonic()
            task = asyncio.create_task(self._await_upload_completion(completion))
            self._upload_tasks.add(task)
            task.add_done_callback(self._upload_tasks.discard)
        finally:
            # Count on arrival (see _on_sync_mtime), after the upload task is
            # registered so a caller that observes completion can wait for it.
            # FileUpload also arrives outside a sync round (the watcher's
            # FileUploadCheck gets the same reply); once the round is complete
            # there is nothing left to count, and counting would only repeat
            # the "sync complete" log.
            if not self._sync_complete:
                self._received_upload += 1
                self._check_complete()

    def _ensure_upload_queue(self) -> asyncio.Queue:
        if self._upload_queue is None:
            self._upload_queue = asyncio.Queue()
        return self._upload_queue

    async def _ensure_upload_workers(self) -> None:
        self._ensure_upload_queue()
        while len(self._upload_workers) < self._upload_worker_count:
            task = asyncio.create_task(self._upload_queue_worker())
            self._upload_workers.add(task)
            task.add_done_callback(self._upload_workers.discard)

    async def _await_upload_completion(self, completion: asyncio.Future) -> None:
        await completion

    async def wait_uploads_idle(
        self,
        progress_timeout: float = 60.0,
        poll_interval: float = 0.1,
    ) -> None:
        """Wait until queued and in-flight uploads finish.

        Uses a progress timeout rather than a total timeout: every chunk sent
        refreshes the progress timestamp, so a large upload is not cut off as
        long as it keeps moving. One-shot commands call this before closing
        the connection so in-flight uploads are not truncated.
        """
        while self._upload_tasks or self._upload_queue_pending():
            idle_for = time.monotonic() - self._last_upload_progress_monotonic
            if idle_for >= progress_timeout:
                log.warning(
                    "Giving up waiting for %d uploads after %.0fs without progress",
                    len(self._upload_tasks), idle_for,
                )
                return
            await asyncio.sleep(poll_interval)

    def _upload_queue_pending(self) -> bool:
        return self._upload_queue is not None and not self._upload_queue.empty()

    async def _upload_queue_worker(self) -> None:
        while True:
            queue = self._ensure_upload_queue()
            session_id, chunk_size, rel_path, full, completion = await queue.get()
            try:
                await self._upload_session_worker(session_id, chunk_size, rel_path, full)
            except Exception as exc:
                if not completion.done():
                    completion.set_exception(exc)
            else:
                if not completion.done():
                    completion.set_result(None)
            finally:
                queue.task_done()

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
            self._last_upload_progress_monotonic = time.monotonic()
            offset = end
            idx += 1
            await asyncio.sleep(0)
        if total == 0:
            chunk = build_binary_chunk(session_id, 0, b"")
            await self.engine.ws_client.send_bytes(chunk)
            self._last_upload_progress_monotonic = time.monotonic()
        log.info("Upload complete: %s (%d chunks)", rel_path, idx)

    async def _on_upload_ack(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        rel_path = data.get("path", "")
        if not rel_path:
            return
        log.debug("← FileUploadAck: %s", rel_path)

        # The server now holds the content we reported (or already held it).
        # Record the *reported* hash/mtime/size, not the on-disk state: the
        # server's row carries the hash we sent, and if the file changed while
        # the upload was in flight the next round must see that difference.
        pending = self._pending_uploads.pop(rel_path, None)
        if pending is None:
            # Ack for a check we never issued in this round (e.g. a broadcast
            # echo); nothing to confirm.
            return
        SyncState.set_baseline(
            self.engine.state.file_baseline,
            rel_path,
            pending["hash"],
            pending["mtime"],
            pending["size"],
        )
        self.engine.state.save()

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
            # a chunked download via FileChunkDownload — unless the local copy
            # already is the content the server is describing. Incremental
            # reporting leaves our own previous uploads out of the request, so
            # the server answers them with FileSyncUpdate; downloading them
            # again would undo the point of reporting less.
            if self._local_matches_attachment(rel_path, data):
                log.debug(
                    "← FileSyncUpdate: %s already matches the local content", rel_path,
                )
                self._received_modify += 1
                self._check_complete()
                return
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
                self._record_baseline(rel_path)
            log.info("← FileSyncUpdate: %s", rel_path)
        except Exception:
            log.exception("Failed to write file %s", rel_path)

        self._received_modify += 1
        self._check_complete()

    def _local_matches_attachment(self, rel_path: str, data: dict) -> bool:
        """Whether the attachment described by FileSyncUpdate is already on disk.

        The server sends contentHash and size for attachments (3.0.0 and
        later), so the local file can be compared before asking for a chunked
        download. Anything unexpected — a missing or mistyped field, no local
        file, a directory, a stat failure — returns False, and the normal
        download path runs unchanged.
        """
        content_hash = data.get("contentHash")
        size = data.get("size")
        if (
            not isinstance(content_hash, str)
            or not content_hash
            or not isinstance(size, int)
            or isinstance(size, bool)
        ):
            return False

        full = self.vault_path / rel_path
        try:
            stat = full.stat()
        except OSError:
            return False
        if not full.is_file() or stat.st_size != size:
            return False

        mtime = int(stat.st_mtime * 1000)
        base = self.engine.state.file_baseline.get(rel_path)
        if (
            base is not None
            and base.get("hash") == content_hash
            and base.get("size") == size
            and base.get("mtime") == mtime
        ):
            # Fast path: the baseline pins this exact content and the file has
            # not been touched since, so the bytes need not be hashed again.
            local_hash = content_hash
        else:
            local_hash = file_content_hash_binary(full)
        if local_hash != content_hash:
            return False

        # Confirmed identical: the server's content is what is on disk, so
        # this path is in the baseline whether it came from the fast path or
        # from hashing. Do not re-hash via _record_baseline here; the fast
        # path exists precisely to avoid reading the file.
        self._pending_uploads.pop(rel_path, None)
        SyncState.set_baseline(
            self.engine.state.file_baseline,
            rel_path,
            content_hash,
            mtime,
            size,
        )
        self._echo_hashes[rel_path] = content_hash
        return True

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
            # The server no longer has this path: drop it from the baseline so
            # a later local recreate is reported as new instead of unchanged.
            SyncState.drop_baseline(self.engine.state.file_baseline, rel_path)
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
                    self._record_baseline(new_path)
                SyncState.drop_baseline(self.engine.state.file_baseline, old_path)
                log.info("← FileSyncRename: %s → %s", old_path, new_path)
        except Exception:
            log.exception("Failed to rename file %s → %s", old_path, new_path)

    async def _on_sync_mtime(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
        # Count the detail on arrival, not on a successful utime: the server
        # counts the frames it sends, so a zero mtime or a missing file must
        # still advance this round towards completion.
        self._received_mtime += 1
        self._check_complete()
        rel_path: str = data.get("path", "")
        mtime = data.get("mtime", 0)
        if not rel_path or not mtime:
            return
        full = self.vault_path / rel_path
        if full.exists():
            try:
                ts = mtime / 1000.0
                os.utime(full, (ts, ts))
                self._record_baseline(rel_path)
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
                self._record_baseline(rel_path)
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
                self._record_baseline(rel_path)
                log.info("← Zero-chunk download complete: %s", rel_path)
        except Exception:
            log.exception("Failed to finalize zero-chunk download %s", rel_path)
        finally:
            self._pending_download_paths.discard(rel_path)
            self._check_complete()

    async def _send_page_ack(self, context: str, page_index: int, vault: str) -> None:
        msg = WSMessage(
            ACTION_FILE_SYNC_PAGE_ACK,
            {
                "context": context,
                "pageIndex": page_index,
                "vault": vault,
            },
        )
        await self.engine.ws_client.send(msg)

    async def _on_sync_page(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        self._mark_sync_activity()
        if isinstance(msg.data, dict):
            page_index = data.get("pageIndex") if data.get("pageIndex") is not None else msg.data.get("pageIndex", 0)
            is_last = data.get("isLast") if data.get("isLast") is not None else msg.data.get("isLast", False)
        else:
            page_index = data.get("pageIndex", 0)
            is_last = data.get("isLast", False)
        page_index = int(page_index)
        is_last = bool(is_last)
        log.debug("← FileSyncPage (pageIndex=%d, isLast=%s)", page_index, is_last)

        if not is_last and page_index >= 0:
            await self._send_page_ack(self._sync_context, page_index, self._sync_vault)

        self._check_complete()

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        if isinstance(msg.data, dict):
            self._sync_context = msg.data.get("context", "") or (data.get("context", "") if isinstance(data, dict) else "") or ""
            self._sync_vault = msg.data.get("vault", "") or (data.get("vault", "") if isinstance(data, dict) else "") or ""
        elif isinstance(data, dict):
            self._sync_context = data.get("context", "") or ""
            self._sync_vault = data.get("vault", "") or ""
        self._mark_sync_activity()
        last_time = data.get("lastTime", 0)
        self._pending_last_time = last_time
        self._expected_modify = int(data.get("needModifyCount") or 0)
        self._expected_delete = int(data.get("needDeleteCount") or 0)
        self._expected_upload = int(data.get("needUploadCount") or 0)
        self._expected_mtime = int(data.get("needSyncMtimeCount") or 0)

        self._got_end = True
        log.info(
            "← FileSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time, self._expected_modify, self._expected_delete, self._expected_upload,
        )

        total_expected = self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete
        if total_expected > 0:
            await self._send_page_ack(self._sync_context, -1, self._sync_vault)

        self._check_complete()

    def _check_complete(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete
        total_received = self._received_upload + self._received_modify + self._received_mtime + self._received_delete
        if total_received >= total_expected and not self._pending_download_paths and not self._download_sessions:
            log.info(
                "FileSync complete: %d uploaded, %d modified, %d mtime, %d deleted",
                self._received_upload, self._received_modify,
                self._received_mtime, self._received_delete,
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
        if not self._pending_last_time:
            return
        log.info("Committing file lastTime=%d", self._pending_last_time)
        self.engine.state.last_file_sync_time = self._pending_last_time
        self._commit_baseline()
        self.engine.state.save()
        self._pending_last_time = 0

    def _record_baseline(self, rel_path: str) -> None:
        """Record the on-disk state after the server confirmed this path."""
        full = self.vault_path / rel_path
        try:
            stat = full.stat()
        except OSError:
            log.debug("Cannot record baseline for %s", rel_path)
            return
        # A download or mtime sync answers the upload check as well; the
        # pending entry is superseded by what is on disk now.
        self._pending_uploads.pop(rel_path, None)
        SyncState.set_baseline(
            self.engine.state.file_baseline,
            rel_path,
            file_content_hash_binary(full),
            int(stat.st_mtime * 1000),
            stat.st_size,
        )

    def _commit_baseline(self) -> None:
        """Fold this round's report into the persisted baseline."""
        if self._baseline_committed:
            return
        self._baseline_committed = True
        committed = commit_round_baseline(
            self.vault_path,
            self.engine.state.file_baseline,
            self._reported,
            self._pending_uploads,
            self._upload_directives,
            self._expected_upload,
        )
        log.info(
            "File baseline committed: %d reported paths, %d total",
            committed,
            len(self.engine.state.file_baseline),
        )

    def _collect_local_files(self) -> list[dict]:
        """Collect local files with hashes for FileSync.

        In incremental mode (a previous sync completed and a baseline exists)
        only files that differ from the baseline are reported. The server
        treats every reported path outside its incremental window as "client
        has this, server may not" and asks for an upload, so reporting
        unchanged files makes it ask for a re-upload on every reconnect.
        Without a baseline (first run, state file written before baselines
        existed) everything is reported as before.

        ``push`` does not go through here: it uploads every file directly, so
        it also serves as the recovery path if the server ever loses files the
        baseline still lists.
        """
        state = self.engine.state
        # Only incremental once a previous round completed (the server's window
        # is meaningful) *and* a baseline exists. Without a baseline every path
        # is reported, which is what the server needs on a first run or after
        # upgrading from a state file written before baselines existed.
        incremental = (
            state.last_file_sync_time > 0
            and bool(state.file_baseline)
        )
        files = []
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel) or rel.endswith(".md"):
                continue
            first = rel.split("/")[0]
            # Skip dot-prefixed directories that are handled by SettingSync
            # (.obsidian, .agents, and other config dirs)
            if first.startswith(".") and self.config.sync.sync_config:
                continue
            if not self.config.sync.sync_files:
                continue
            try:
                stat = fp.stat()
                entry = {
                    "path": rel,
                    "pathHash": path_hash(rel),
                    "contentHash": file_content_hash_binary(fp),
                    "mtime": int(stat.st_mtime * 1000),
                    "ctime": int(stat.st_ctime * 1000),
                    "size": stat.st_size,
                }
            except Exception:
                log.debug("Failed to hash file %s, skipping", rel)
                continue
            if incremental and baseline_matches(
                state.file_baseline.get(rel), entry["contentHash"], entry["size"]
            ):
                continue
            files.append(entry)
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
