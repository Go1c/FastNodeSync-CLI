"""Persistent sync state (lastSyncTime, incremental-report baseline)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

from .hash_utils import file_content_hash_binary

DEFAULT_STATE_FILE = ".fns_state.json"


def _load_baseline(raw: object) -> Dict[str, dict]:
    """Validate a baseline table read from the state file.

    Malformed entries are dropped instead of failing the load, so a
    hand-edited or partially written state file still starts the client.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, dict] = {}
    for path, entry in raw.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            continue
        content_hash = entry.get("hash")
        mtime = entry.get("mtime")
        size = entry.get("size", 0)
        if not isinstance(content_hash, str) or not isinstance(mtime, int):
            continue
        out[path] = {
            "hash": content_hash,
            "mtime": mtime,
            "size": size if isinstance(size, int) else 0,
        }
    return out


def baseline_matches(base: dict | None, content_hash: str, size: int) -> bool:
    """Whether a local file still equals the state the server confirmed.

    Compared by content hash (plus size as a collision guard), not mtime: a
    backup or extraction tool that refreshes mtime without changing the bytes
    must not turn into a re-upload. If the server's copy did change meanwhile,
    its row is inside the incremental window and the server pushes it down
    regardless of what the client reports.
    """
    if not base:
        return False
    return base.get("hash") == content_hash and base.get("size") == size


def commit_round_baseline(
    vault_path: Path,
    table: Dict[str, dict],
    reported: Dict[str, dict],
    pending_uploads: Dict[str, dict],
    upload_directives: set,
    need_upload: int,
) -> int:
    """Fold a finished round's report into ``table`` (in place).

    A reported path is recorded as "the server has it" only when the server
    did not ask for it. Paths that are still waiting for an upload ack, or
    that the server explicitly asked to upload, keep whatever the previous
    round established: the content is not known to be on the server yet.

    The server announces the number of uploads it wants in ``*SyncEnd`` but
    delivers the directives themselves through the paged queue, which this
    client only pulls when the round also has modify/delete items. When fewer
    directives arrived than announced, it is impossible to tell which reported
    paths the server meant, so nothing from this round is committed.

    Returns the number of paths written.
    """
    for path in list(table):
        if path in reported or (vault_path / path).exists():
            continue
        del table[path]

    delivered = len(upload_directives & reported.keys())
    if need_upload > delivered:
        return 0

    committed = 0
    for path, entry in reported.items():
        if path in pending_uploads or path in upload_directives:
            continue
        full = vault_path / path
        try:
            stat = full.stat()
        except OSError:
            # Deleted while the round ran; the next round reports it.
            continue
        if file_content_hash_binary(full) != entry["contentHash"]:
            # Changed while the round ran; let the next round see it.
            continue
        table[path] = {
            "hash": entry["contentHash"],
            "mtime": entry["mtime"],
            "size": entry["size"],
        }
        committed += 1
    return committed


@dataclass
class SyncState:
    last_note_sync_time: int = 0
    last_file_sync_time: int = 0
    last_setting_sync_time: int = 0
    # Paths of notes successfully synced in the last full sync.
    # Used to detect local deletions: any path here that no longer exists on
    # disk was deleted and should be sent to the server as delNotes.
    synced_note_paths: List[str] = field(default_factory=list)
    # Per-path baseline of the last state the server confirmed, for attachments
    # (file_baseline) and config files (setting_baseline):
    #   {path: {"hash": <content hash>, "mtime": <epoch ms>, "size": <bytes>}}
    # A path with no entry is always reported, so a state file written before
    # baselines existed keeps the old report-everything behaviour until a
    # round fills the table in.
    file_baseline: Dict[str, dict] = field(default_factory=dict)
    setting_baseline: Dict[str, dict] = field(default_factory=dict)

    _path: str = field(default="", repr=False)

    def save(self) -> None:
        if not self._path:
            return
        d = asdict(self)
        d.pop("_path", None)
        Path(self._path).write_text(json.dumps(d, indent=2), encoding="utf-8")

    @staticmethod
    def set_baseline(
        table: Dict[str, dict],
        path: str,
        content_hash: str,
        mtime: int,
        size: int,
    ) -> None:
        table[path] = {
            "hash": content_hash,
            "mtime": int(mtime),
            "size": int(size),
        }

    @staticmethod
    def drop_baseline(table: Dict[str, dict], path: str) -> None:
        table.pop(path, None)

    @classmethod
    def load(cls, vault_dir: Path) -> SyncState:
        p = vault_dir / DEFAULT_STATE_FILE
        state = cls(_path=str(p))
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                state.last_note_sync_time = raw.get("last_note_sync_time", 0)
                state.last_file_sync_time = raw.get("last_file_sync_time", 0)
                state.last_setting_sync_time = raw.get("last_setting_sync_time", 0)
                state.synced_note_paths = raw.get("synced_note_paths", [])
                state.file_baseline = _load_baseline(raw.get("file_baseline"))
                state.setting_baseline = _load_baseline(raw.get("setting_baseline"))
            except (json.JSONDecodeError, OSError):
                pass
        return state
