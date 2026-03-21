"""WebSocket message encoding / decoding and Action constants."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SEPARATOR = "|"


# ── Client → Server actions ──────────────────────────────────────────
ACTION_AUTHORIZATION = "Authorization"
ACTION_CLIENT_INFO = "ClientInfo"

ACTION_NOTE_SYNC = "NoteSync"
ACTION_NOTE_MODIFY = "NoteModify"
ACTION_NOTE_DELETE = "NoteDelete"
ACTION_NOTE_RENAME = "NoteRename"
ACTION_NOTE_CHECK = "NoteCheck"
ACTION_NOTE_RE_PUSH = "NoteRePush"

ACTION_FILE_SYNC = "FileSync"
ACTION_FILE_UPLOAD_CHECK = "FileUploadCheck"
ACTION_FILE_DELETE = "FileDelete"
ACTION_FILE_CHUNK_DOWNLOAD = "FileChunkDownload"

ACTION_FOLDER_SYNC = "FolderSync"
ACTION_FOLDER_MODIFY = "FolderModify"
ACTION_FOLDER_DELETE = "FolderDelete"
ACTION_FOLDER_RENAME = "FolderRename"

# ── Server → Client actions ──────────────────────────────────────────
ACTION_NOTE_SYNC_MODIFY = "NoteSyncModify"
ACTION_NOTE_SYNC_DELETE = "NoteSyncDelete"
ACTION_NOTE_SYNC_RENAME = "NoteSyncRename"
ACTION_NOTE_SYNC_MTIME = "NoteSyncMtime"
ACTION_NOTE_SYNC_NEED_PUSH = "NoteSyncNeedPush"
ACTION_NOTE_SYNC_END = "NoteSyncEnd"

ACTION_FILE_SYNC_UPDATE = "FileSyncUpdate"
ACTION_FILE_SYNC_DELETE = "FileSyncDelete"
ACTION_FILE_SYNC_RENAME = "FileSyncRename"
ACTION_FILE_SYNC_MTIME = "FileSyncMtime"
ACTION_FILE_SYNC_CHUNK_DOWNLOAD = "FileSyncChunkDownload"
ACTION_FILE_UPLOAD = "FileUpload"
ACTION_FILE_SYNC_END = "FileSyncEnd"

ACTION_FOLDER_SYNC_END = "FolderSyncEnd"

# ── Status codes ──────────────────────────────────────────────────────
CODE_SUCCESS = 1
CODE_NO_UPDATE = 6
CODE_SUCCESS_ALT = 200
CODE_PARAM_ERROR = 305
CODE_NOTE_SAVE_FAIL = 433
CODE_CONTENT_CONFLICT = 441
CODE_UPLOAD_SESSION_INVALID = 463
CODE_SYNC_CONFLICT = 490


@dataclass
class WSMessage:
    action: str
    data: Any

    def encode(self) -> str:
        if isinstance(self.data, str):
            payload = json.dumps(self.data)
        else:
            payload = json.dumps(self.data, ensure_ascii=False)
        return f"{self.action}{SEPARATOR}{payload}"


def decode_message(raw: str) -> WSMessage:
    idx = raw.find(SEPARATOR)
    if idx == -1:
        return WSMessage(action=raw, data={})
    action = raw[:idx]
    json_str = raw[idx + 1:]
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        data = json_str
    return WSMessage(action=action, data=data)


def build_binary_chunk(session_id: str, chunk_index: int, data: bytes) -> bytes:
    """Build the binary frame: b'BC' + sessionId(36B) + chunkIndex(4B BE) + data."""
    prefix = b"BC"
    sid_bytes = session_id.encode("ascii")[:36].ljust(36, b"\x00")
    idx_bytes = chunk_index.to_bytes(4, byteorder="big")
    return prefix + sid_bytes + idx_bytes + data


def parse_binary_chunk(raw: bytes) -> tuple[str, int, bytes]:
    """Parse a binary frame → (session_id, chunk_index, data)."""
    sid = raw[2:38].decode("ascii").rstrip("\x00")
    chunk_index = int.from_bytes(raw[38:42], byteorder="big")
    return sid, chunk_index, raw[42:]
