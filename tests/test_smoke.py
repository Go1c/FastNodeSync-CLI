"""Lightweight smoke tests (stdlib unittest, no extra deps)."""

from __future__ import annotations

import unittest


class TestImports(unittest.TestCase):
    def test_package_has_version(self) -> None:
        import fns_cli

        self.assertTrue(isinstance(fns_cli.__version__, str))
        self.assertGreater(len(fns_cli.__version__), 0)

    def test_hash_matches_plugin_algorithm(self) -> None:
        from fns_cli.hash_utils import hash_content

        self.assertEqual(hash_content("hello"), "99162322")

    def test_protocol_encode_decode(self) -> None:
        from fns_cli.protocol import WSMessage, decode_message

        raw = WSMessage("TestAction", {"a": 1}).encode()
        msg = decode_message(raw)
        self.assertEqual(msg.action, "TestAction")
        self.assertEqual(msg.data, {"a": 1})


if __name__ == "__main__":
    unittest.main()
