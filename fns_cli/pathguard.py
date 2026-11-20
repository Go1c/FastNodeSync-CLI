"""Lexical safety check for paths received from the server."""

from __future__ import annotations

import os
import re

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def is_safe_rel_path(rel_path: str, *, windows: bool | None = None) -> bool:
    """Lexical check: relative, no '..' segment, no drive/root prefix. Does not resolve symlinks.

    A drive prefix ("C:") is only dangerous on Windows, where it is a real
    path root. On POSIX systems "C:note.md" is an ordinary file name inside
    the vault, so it stays allowed there.
    """
    if windows is None:
        windows = os.name == "nt"
    if not isinstance(rel_path, str) or not rel_path:
        return False
    if "\x00" in rel_path:
        return False
    if rel_path.startswith(("/", "\\")):
        return False
    if windows and _DRIVE_PREFIX.match(rel_path):
        return False
    normalized = rel_path.replace("\\", "/")
    return ".." not in normalized.split("/")
