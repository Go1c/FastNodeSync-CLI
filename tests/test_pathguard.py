"""Unit tests for the lexical server-path safety check."""

from __future__ import annotations

import unittest

from fns_cli.pathguard import is_safe_rel_path


class TestIsSafeRelPath(unittest.TestCase):

    def test_plain_relative_paths_are_safe(self):
        for path in ("note.md", "folder/note.md", ".obsidian/app.json", "a\\b.txt"):
            with self.subTest(path=path):
                self.assertTrue(is_safe_rel_path(path))

    def test_dot_segments_other_than_dotdot_are_safe(self):
        for path in ("a/./b.txt", "..hidden/x.md", "a..b/c"):
            with self.subTest(path=path):
                self.assertTrue(is_safe_rel_path(path))

    def test_empty_and_none_are_unsafe(self):
        self.assertFalse(is_safe_rel_path(""))

    def test_absolute_paths_are_unsafe(self):
        for path in ("/etc/passwd", "\\windows\\system32", "/a/b"):
            with self.subTest(path=path):
                self.assertFalse(is_safe_rel_path(path))

    def test_drive_prefix_is_unsafe(self):
        for path in ("C:temp", "c:\\temp\\x", "Z:"):
            with self.subTest(path=path):
                self.assertFalse(is_safe_rel_path(path))

    def test_parent_segments_are_unsafe(self):
        for path in ("../secret", "a/../../b", "a/..", "..\\secret", "a\\..\\b", ".."):
            with self.subTest(path=path):
                self.assertFalse(is_safe_rel_path(path))

    def test_nul_byte_is_unsafe(self):
        self.assertFalse(is_safe_rel_path("a\x00b"))


if __name__ == "__main__":
    unittest.main()
