"""Setting (config) sync protocol: SettingSync incremental pull + SettingModify/SettingDelete push."""

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
from .state import SyncState, baseline_matches, commit_round_baseline
from .protocol import (
    ACTION_SETTING_DELETE,
    ACTION_SETTING_MODIFY,
    ACTION_SETTING_MODIFY_ACK,
    ACTION_SETTING_SYNC,
    ACTION_SETTING_SYNC_DELETE,
    ACTION_SETTING_SYNC_END,
    ACTION_SETTING_SYNC_MODIFY,
    ACTION_SETTING_SYNC_MTIME,
    ACTION_SETTING_SYNC_NEED_UPLOAD,
    ACTION_SETTING_SYNC_PAGE,
    ACTION_SETTING_SYNC_PAGE_ACK,
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


def _is_config_path(
    rel: str,
    config_sync_dirs: list[str] | None = None,
    sync_config: bool = True,
) -> bool:
    """Check whether a relative path belongs to config/settings scope.

    Explicit config sync directories (e.g. .obsidian, .agents) are always
    treated as settings. Other dot-prefixed directories follow sync_config.
    Standard exclusions (.git, .trash) are handled by is_excluded() upstream.

    config_sync_dirs: configured dot-prefixed directories to treat as config
                      (from config.yaml, e.g. [".obsidian", ".agents"])
    """
    first = rel.split("/")[0]
    if not first.startswith("."):
        return False
    # Check configured config sync directories
    if config_sync_dirs and first in config_sync_dirs:
        return True
    # Other dot-prefixed dirs only count as config when config sync is enabled.
    return sync_config


class SettingSync:
    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.config = engine.config
        self.vault_path = engine.vault_path
        self._sync_complete = False
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
        self._sync_context = ""
        self._sync_vault = ""
        self._echo_hashes: dict[str, str] = {}
        # Same role as the identically named FileSync attributes: see the
        # comments in FileSync.__init__ and FileSync._commit_baseline.
        self._reported: dict[str, dict] = {}
        self._pending_uploads: dict[str, dict] = {}
        self._upload_directives: set[str] = set()
        self._baseline_committed = False
        self._last_ack_progress_monotonic = time.monotonic()

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
        ws.on(ACTION_SETTING_MODIFY_ACK, self._on_modify_ack)
        ws.on(ACTION_SETTING_SYNC_PAGE, self._on_sync_page)
        ws.on(ACTION_SETTING_SYNC_END, self._on_sync_end)

    async def request_sync(self) -> None:
        """Send incremental SettingSync request."""
        self._reset_counters()
        last_time = self.engine.state.last_setting_sync_time
        ctx = str(uuid.uuid4())
        settings = self._collect_local_settings()
        self._reported = {entry["path"]: entry for entry in settings}
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
        settings = self._collect_local_settings(full=True)
        self._reported = {entry["path"]: entry for entry in settings}
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
        self._pending_uploads[rel_path] = {
            "hash": hash_,
            "mtime": int(stat.st_mtime * 1000),
            "size": stat.st_size,
        }
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

    async def wait_modify_acks(
        self,
        ack_timeout: float = 10.0,
        poll_interval: float = 0.1,
    ) -> None:
        """Wait, bounded, for SettingModifyAck of config uploads already sent.

        One-shot commands are about to close the connection and the baseline
        is recorded on the ack. Servers from 3.2.1 answer an unchanged
        SettingModify with a SettingModifyAck that carries the path; 3.0.x and
        3.1.x answer with a bare success that names neither an action nor a
        path, so for those no ack will ever match. The wait therefore uses a
        progress timeout and just returns when nothing new arrives — that is
        normal for older servers, not an error.
        """
        while self._pending_uploads:
            idle_for = time.monotonic() - self._last_ack_progress_monotonic
            if idle_for >= ack_timeout:
                log.debug(
                    "Giving up waiting for %d setting acks after %.0fs without progress",
                    len(self._pending_uploads), idle_for,
                )
                return
            await asyncio.sleep(poll_interval)

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
            self._record_baseline(rel_path)
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
            SyncState.drop_baseline(self.engine.state.setting_baseline, rel_path)
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
                    self._record_baseline(new_path)
                SyncState.drop_baseline(self.engine.state.setting_baseline, old_path)
                log.info("← SettingSyncRename: %s → %s", old_path, new_path)
                self._try_remove_empty_parent(old_full)
        except Exception:
            log.exception("Failed to rename setting %s → %s", old_path, new_path)

    async def _on_sync_mtime(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        # Count the detail on arrival, not on a successful utime: the server
        # counts the frames it sends, so a zero mtime or a missing file must
        # still advance this round towards completion.
        self._received_mtime += 1
        self._check_all_received()
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

    async def _on_modify_ack(self, msg: WSMessage) -> None:
        """SettingModifyAck: the server accepted (or already held) our content."""
        data = _extract_inner(msg.data)
        rel_path = data.get("path", "")
        if not rel_path:
            return
        log.debug("← SettingModifyAck: %s", rel_path)
        pending = self._pending_uploads.pop(rel_path, None)
        if pending is None:
            return
        # One-shot commands may be waiting for these acks before closing.
        self._last_ack_progress_monotonic = time.monotonic()
        SyncState.set_baseline(
            self.engine.state.setting_baseline,
            rel_path,
            pending["hash"],
            pending["mtime"],
            pending["size"],
        )
        self.engine.state.save()

    async def _on_sync_need_upload(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        need_upload = data.get("needUpload", [])
        if isinstance(need_upload, list) and need_upload:
            log.info("← SettingSyncNeedUpload: %d files", len(need_upload))
            # Legacy list form: one count per entry. If no entry yields a
            # path, count the frame once so the round can still complete.
            paths = [
                item.get("path", "") if isinstance(item, dict) else str(item)
                for item in need_upload
            ]
            paths = [p for p in paths if p]
            try:
                for rel_path in paths:
                    if not is_safe_rel_path(rel_path):
                        log.warning("Ignoring unsafe setting upload path: %s", rel_path)
                        continue
                    # The server named this path in an upload directive: it does
                    # not consider the reported content to be in sync.
                    self._upload_directives.add(rel_path)
                    await self.push_modify(rel_path, force=True)
            finally:
                if not self._sync_complete:
                    self._received_upload += max(len(paths), 1)
                    self._check_all_received()
            return

        rel_path = data.get("path", "")
        try:
            if not rel_path:
                return
            if not is_safe_rel_path(rel_path):
                log.warning("Ignoring unsafe setting upload path: %s", rel_path)
                return
            log.info("← SettingSyncNeedUpload: %s", rel_path)
            # The server named this path in an upload directive: it does not
            # consider the reported content to be in sync.
            self._upload_directives.add(rel_path)
            # An explicit upload request from the server must bypass echo
            # suppression, same as NoteSync's NeedPush handler.
            await self.push_modify(rel_path, force=True)
        finally:
            # Count on arrival (see _on_sync_mtime): a detail the server sent
            # must advance this round even if we cannot act on it. Once the
            # round is complete there is nothing left to count.
            if not self._sync_complete:
                self._received_upload += 1
                self._check_all_received()

    async def _on_sync_end(self, msg: WSMessage) -> None:
        data = _extract_inner(msg.data)
        if isinstance(msg.data, dict):
            self._sync_context = (msg.data.get("context") or (data.get("context") if isinstance(data, dict) else "") or "")
            self._sync_vault = (msg.data.get("vault") or (data.get("vault") if isinstance(data, dict) else "") or "")
        elif isinstance(data, dict):
            self._sync_context = data.get("context") or ""
            self._sync_vault = data.get("vault") or ""

        last_time = data.get("lastTime", 0)
        self._expected_modify = int(data.get("needModifyCount") or 0)
        self._expected_delete = int(data.get("needDeleteCount") or 0)
        self._expected_upload = int(data.get("needUploadCount") or 0)
        self._expected_mtime = int(data.get("needSyncMtimeCount") or 0)
        self._pending_last_time = last_time
        self._got_end = True
        log.info(
            "← SettingSyncEnd (lastTime=%d, needModify=%d, needDelete=%d, needUpload=%d)",
            last_time,
            self._expected_modify,
            self._expected_delete,
            data.get("needUploadCount", 0),
        )

        total_expected = self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete
        if total_expected == 0:
            self._sync_complete = True
            self._commit_last_time()
        else:
            await self._send_page_ack(self._sync_context, -1, self._sync_vault)
            self._check_all_received()

    async def _send_page_ack(self, context: str, page_index: int, vault: str) -> None:
        msg = WSMessage(
            ACTION_SETTING_SYNC_PAGE_ACK,
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
        log.debug("← SettingSyncPage (pageIndex=%d, isLast=%s)", page_index, is_last)

        if not is_last and page_index >= 0:
            await self._send_page_ack(self._sync_context, page_index, self._sync_vault)

        self._check_all_received()

    # ── Internal helpers ─────────────────────────────────────────────

    def _reset_counters(self) -> None:
        self._sync_complete = False
        self._got_end = False
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
        self._reported = {}
        self._pending_uploads = {}
        self._upload_directives = set()
        self._baseline_committed = False

    def _check_all_received(self) -> None:
        if not self._got_end:
            return
        total_expected = self._expected_upload + self._expected_modify + self._expected_mtime + self._expected_delete
        total_received = self._received_upload + self._received_modify + self._received_mtime + self._received_delete
        if total_received >= total_expected:
            log.info(
                "SettingSync complete: %d uploaded, %d modified, %d mtime, %d deleted",
                self._received_upload,
                self._received_modify,
                self._received_mtime,
                self._received_delete,
            )
            self._sync_complete = True
            self._commit_last_time()

    def _commit_last_time(self) -> None:
        if not self._pending_last_time:
            return
        log.info("Committing setting lastTime=%d", self._pending_last_time)
        self.engine.state.last_setting_sync_time = self._pending_last_time
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
            self.engine.state.setting_baseline,
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
            self.engine.state.setting_baseline,
            self._reported,
            self._pending_uploads,
            self._upload_directives,
            self._expected_upload,
        )
        log.info(
            "Setting baseline committed: %d reported paths, %d total",
            committed,
            len(self.engine.state.setting_baseline),
        )

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

    def _collect_local_settings(self, full: bool = False) -> list[dict]:
        """Collect config files under dot-prefixed directories.

        In incremental mode (a previous sync completed and a baseline exists)
        only files that differ from the baseline are reported; see
        FileSync._collect_local_files for why. ``request_full_sync`` reports
        everything (``lastTime=0``), which also serves as the recovery path.
        """
        state = self.engine.state
        incremental = (
            not full
            and state.last_setting_sync_time > 0
            and bool(state.setting_baseline)
        )
        settings = []
        for fp in self.vault_path.rglob("*"):
            if fp.is_dir():
                continue
            rel = fp.relative_to(self.vault_path).as_posix()
            if self.engine.is_excluded(rel):
                continue
            if not _is_config_path(
                rel,
                self.config.sync.config_sync_dirs,
                self.config.sync.sync_config,
            ):
                continue
            try:
                hash_ = file_content_hash_binary(fp)
            except Exception:
                continue
            stat = fp.stat()
            entry = {
                "path": rel,
                "pathHash": path_hash(rel),
                "contentHash": hash_,
                "mtime": int(stat.st_mtime * 1000),
                "ctime": int(stat.st_ctime * 1000),
                "size": stat.st_size,
            }
            if incremental and baseline_matches(
                state.setting_baseline.get(rel), entry["contentHash"], entry["size"]
            ):
                continue
            settings.append(entry)
        return settings
