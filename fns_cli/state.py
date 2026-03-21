"""Persistent sync state (lastSyncTime etc.)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_STATE_FILE = ".fns_state.json"


@dataclass
class SyncState:
    last_note_sync_time: int = 0
    last_file_sync_time: int = 0

    _path: str = field(default="", repr=False)

    def save(self) -> None:
        if not self._path:
            return
        d = asdict(self)
        d.pop("_path", None)
        Path(self._path).write_text(json.dumps(d, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, vault_dir: Path) -> SyncState:
        p = vault_dir / DEFAULT_STATE_FILE
        state = cls(_path=str(p))
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                state.last_note_sync_time = raw.get("last_note_sync_time", 0)
                state.last_file_sync_time = raw.get("last_file_sync_time", 0)
            except (json.JSONDecodeError, OSError):
                pass
        return state
