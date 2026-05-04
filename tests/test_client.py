"""Unit tests for WSClient reconnect behaviour."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if "yaml" not in sys.modules:
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda _stream: {}
    sys.modules["yaml"] = yaml_stub

from fns_cli.client import WSClient
from fns_cli.protocol import ACTION_CLIENT_INFO, WSMessage


def _make_config() -> MagicMock:
    config = MagicMock()
    config.client.reconnect_base_delay = 1
    config.client.reconnect_max_retries = 3
    config.server.token = "token"
    config.ws_api = "wss://example.com"
    return config


class TestWSClientReconnect(unittest.IsolatedAsyncioTestCase):

    async def test_auth_response_does_not_block_on_reconnect_sync(self):
        client = WSClient(_make_config())
        client._connect_count = 2
        client._raw_send = AsyncMock()
        client._flush_queue = AsyncMock()

        started = asyncio.Event()
        release = asyncio.Event()

        async def on_reconnect():
            started.set()
            await release.wait()

        client.on_reconnect(on_reconnect)

        await client._on_auth_response(WSMessage("Authorization", {"code": 1}))

        self.assertTrue(client.is_authenticated)
        self.assertTrue(client._ready_event.is_set())
        self.assertIsNotNone(client._reconnect_task)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertFalse(client._reconnect_task.done())

        release.set()
        await asyncio.wait_for(client._reconnect_task, timeout=1)

    async def test_raw_send_is_serialized(self):
        client = WSClient(_make_config())

        active = 0
        max_active = 0
        release = asyncio.Event()

        class FakeWS:
            async def send(self, _data):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                await release.wait()
                active -= 1

        client.ws = FakeWS()

        first = asyncio.create_task(client._raw_send("a"))
        await asyncio.sleep(0)
        second = asyncio.create_task(client._raw_send("b"))
        await asyncio.sleep(0.05)

        self.assertEqual(max_active, 1)

        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    async def test_client_info_echo_is_ignored(self):
        client = WSClient(_make_config())
        raw = WSMessage(ACTION_CLIENT_INFO, {"name": "server"}).encode()

        with patch("fns_cli.client.log.warning") as warning:
            await client._handle_text(raw)

        warning.assert_not_called()


class TestWSClientLoopBinding(unittest.TestCase):

    def test_wait_ready_uses_running_loop_when_client_created_before_loop(self):
        client = WSClient(_make_config())

        async def probe():
            self.assertFalse(await client.wait_ready(timeout=0.01))

        asyncio.run(probe())


if __name__ == "__main__":
    unittest.main()
