"""Unit tests for SyncEngine.is_excluded path handling."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fns_cli.sync_engine import SyncEngine

DEFAULT_PATTERNS = [".git/**", ".trash/**", "*.tmp"]


def _make_engine(patterns: list[str] | None = None) -> SyncEngine:
    engine = SyncEngine.__new__(SyncEngine)
    config = MagicMock()
    config.sync.exclude_patterns = list(
        DEFAULT_PATTERNS if patterns is None else patterns
    )
    engine.config = config
    return engine


class TestIsExcludedDefaults(unittest.TestCase):
    """Ordinary local relative paths keep their current behaviour."""

    def setUp(self) -> None:
        self.engine = _make_engine()

    def test_normal_paths_are_unchanged(self):
        cases = {
            "note.md": False,
            "folder/note.md": False,
            "folder/sub/deep/note.md": False,
            "中文笔记/子目录/笔记.md": False,
            "my notes/with space.md": False,
            "notes/v1.2.3.md": False,
            "archive/2024.01.02.txt": False,
            ".obsidian/app.json": False,
            ".obsidian/plugins/foo/main.js": False,
            "notes/a.tmp": True,
            "notes/a.tmp.bak": False,
            "notes/temp.tmp": True,
            ".git/config": True,
            ".git/objects/ab/cdef": True,
            ".trash/old.md": True,
            ".trash/sub/deep.bin": True,
            ".trashcan/note.md": False,
            ".fns_state.json": True,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(self.engine.is_excluded(path), expected)

    def test_empty_path_is_not_excluded(self):
        self.assertFalse(self.engine.is_excluded(""))


class TestIsExcludedDirectoryItself(unittest.TestCase):
    """A pattern ending in /** also excludes the directory itself."""

    def test_pattern_with_double_star_excludes_directory_itself(self):
        engine = _make_engine()
        for path in (".git", ".trash", ".git/", ".trash/"):
            with self.subTest(path=path):
                self.assertTrue(engine.is_excluded(path))

    def test_directory_prefix_matching_stays_literal(self):
        engine = _make_engine()
        for path in (".gitlab", ".trashcan", ".trashx/y"):
            with self.subTest(path=path):
                self.assertFalse(engine.is_excluded(path))


class TestIsExcludedUnsafePaths(unittest.TestCase):
    """Paths that escape the vault are always excluded."""

    def setUp(self) -> None:
        self.engine = _make_engine([".trash/**", "*.tmp"])

    def test_absolute_and_drive_paths_are_excluded(self):
        for path in (
            "/etc/passwd",
            "/absolute/note.md",
            "\\server\\share\\note.md",
            "\\absolute",
            "C:\\Windows\\note.md",
            "C:/Windows/note.md",
            "C:note.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.engine.is_excluded(path))

    def test_parent_directory_segments_are_excluded(self):
        for path in (
            "..",
            "../outside.md",
            "../../outside.md",
            "folder/../outside.md",
            "folder/..",
            "folder/../.trash/x",
            "folder\\..\\outside.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.engine.is_excluded(path))

    def test_nul_bytes_are_excluded(self):
        self.assertTrue(self.engine.is_excluded("notes\x00/evil.md"))

    def test_dot_prefix_in_filename_is_not_a_parent_segment(self):
        for path in ("notes/..hidden.md", "notes/a..b.md", "notes/.tmpfile"):
            with self.subTest(path=path):
                self.assertFalse(self.engine.is_excluded(path))


class TestIsExcludedNormalization(unittest.TestCase):
    """Paths are normalised before pattern matching."""

    def setUp(self) -> None:
        self.engine = _make_engine()

    def test_redundant_segments_are_normalized(self):
        for path in (
            "./.trash/x",
            ".trash//x",
            ".trash/x/",
            ".git//config",
            "./.git/config",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.engine.is_excluded(path))

    def test_non_excluded_normalized_paths_stay_visible(self):
        for path in ("./notes/note.md", "notes//note.md", ".obsidian/app.json/"):
            with self.subTest(path=path):
                self.assertFalse(self.engine.is_excluded(path))


if __name__ == "__main__":
    unittest.main()
