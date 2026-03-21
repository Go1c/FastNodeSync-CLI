"""Hash utilities matching the Obsidian plugin's djb2-style algorithm."""

import hashlib
from pathlib import Path


def hash_content(content: str) -> str:
    """Compute a 32-bit signed integer hash identical to the Obsidian plugin.

    Algorithm: djb2 variant — ``h = (h << 5) - h + charCode`` kept as a
    signed 32-bit integer, returned as a decimal string.
    """
    h = 0
    for ch in content:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return str(h)


def path_hash(relative_path: str) -> str:
    return hash_content(relative_path)


def content_hash(text: str) -> str:
    return hash_content(text)


def file_content_hash_binary(file_path: Path) -> str:
    """SHA-256 hex digest for binary files (used by FileSync contentHash)."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()
