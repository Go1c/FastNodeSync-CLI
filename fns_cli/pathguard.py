"""Lexical safety check for paths received from the server."""

from __future__ import annotations

import re

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def is_safe_rel_path(rel_path: str) -> bool:
    """Lexical check: relative, no '..' segment, no drive/root prefix. Does not resolve symlinks."""
    if not isinstance(rel_path, str) or not rel_path:
        return False
    if "\x00" in rel_path:
        return False
    if rel_path.startswith(("/", "\\")):
        return False
    if _DRIVE_PREFIX.match(rel_path):
        return False
    normalized = rel_path.replace("\\", "/")
    return ".." not in normalized.split("/")
