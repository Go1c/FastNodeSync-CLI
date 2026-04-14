"""WebSocket client: connect, authenticate, send/receive, reconnect."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import ClientConnection

from .config import AppConfig
from .protocol import (
    ACTION_AUTHORIZATION,
    ACTION_CLIENT_INFO,
    SEPARATOR,
    WSMessage,
    decode_message,
    parse_binary_chunk,
)

log = logging.getLogger("fns_cli.client")


class WSClient:
    """Async WebSocket client with auth, reconnect, and message dispatch."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ws: ClientConnection | None = None
        self.is_authenticated = False
        self._connect_count = 0
        self._running = False
        self._handlers: dict[str, Callable[..., Coroutine]] = {}
        self._binary_handler: Callable[..., Coroutine] | None = None
        self._on_reconnect: Callable[[], Coroutine] | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._msg_queue: list[str | bytes] = []
        self._ready_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    def on_reconnect(self, handler: Callable[[], Coroutine]) -> None:
        self._on_reconnect = handler

    def on(self, action: str, handler: Callable[..., Coroutine]) -> None:
        self._handlers[action] = handler

    def on_binary(self, handler: Callable[..., Coroutine]) -> None:
        self._binary_handler = handler

    async def send(self, msg: WSMessage) -> None:
        raw = msg.encode()
        if not self.is_authenticated and msg.action != ACTION_AUTHORIZATION:
            self._msg_queue.append(raw)
            return
        await self._raw_send(raw)

    async def send_bytes(self, data: bytes) -> None:
        if not self.is_authenticated:
            self._msg_queue.append(data)
            return
        await self._raw_send(data)

    async def _raw_send(self, data: str | bytes) -> None:
        async with self._send_lock:
            if self.ws is None:
                self._msg_queue.append(data)
                return
            try:
                await self.ws.send(data)
            except websockets.ConnectionClosed:
                self._msg_queue.append(data)

    async def _flush_queue(self) -> None:
        queue, self._msg_queue = self._msg_queue, []
        for msg in queue:
            await self._raw_send(msg)

    async def run(self) -> None:
        self._running = True
        retries = 0
        base_delay = self.config.client.reconnect_base_delay
        max_retries = self.config.client.reconnect_max_retries

        while self._running:
            try:
                await self._connect()
                retries = 0
                await self._listen()
            except (
                websockets.ConnectionClosed,
                ConnectionError,
                OSError,
            ) as exc:
                log.warning("Connection lost: %s", exc)
            except asyncio.CancelledError:
                break

            if not self._running:
                break

            retries += 1
            if retries > max_retries:
                log.error(
                    "Max reconnect retries (%d) exceeded, waiting 60s before reset",
                    max_retries,
                )
                await asyncio.sleep(60)
                retries = 0
                continue

            delay = min(base_delay * (2 ** (retries - 1)), 300)
            log.info("Reconnecting in %ds (attempt %d/%d)", delay, retries, max_retries)
            await asyncio.sleep(delay)

    async def _connect(self) -> None:
        self.is_authenticated = False
        self._ready_event.clear()
        self._connect_count += 1

        url = (
            f"{self.config.ws_api}/api/user/sync"
            f"?lang=zh-cn&count={self._connect_count}"
        )
        log.info("Connecting to %s", url)

        # The server already sends keepalive pings and will close dead
        # connections on its side. During large attachment uploads, our own
        # client-initiated ping/pong cycle causes false timeouts and forces an
        # expensive reconnect + full resync loop, which then delays realtime
        # local pushes even further. Rely on the server heartbeat here.
        self.ws = await websockets.connect(
            url,
            max_size=128 * 1024 * 1024,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=10,
        )
        log.info("WebSocket connected, sending auth")

        auth_raw = f"{ACTION_AUTHORIZATION}{SEPARATOR}{self.config.server.token}"
        await self._raw_send(auth_raw)

    async def _listen(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            if isinstance(raw, bytes):
                await self._handle_binary(raw)
            else:
                await self._handle_text(raw)

    async def _handle_text(self, raw: str) -> None:
        msg = decode_message(raw)
        log.debug("← %s | %s", msg.action, str(msg.data)[:200])

        if msg.action == ACTION_AUTHORIZATION:
            await self._on_auth_response(msg)
            return

        handler = self._handlers.get(msg.action)
        if handler:
            try:
                await handler(msg)
            except Exception:
                log.exception("Handler error for %s", msg.action)
        else:
            log.warning("Unhandled action from server: %s", msg.action)

    async def _handle_binary(self, raw: bytes) -> None:
        if self._binary_handler and len(raw) > 42 and raw[:2] == b"00":
            try:
                sid, idx, data = parse_binary_chunk(raw[2:])
                await self._binary_handler(sid, idx, data)
            except Exception:
                log.exception("Binary handler error")

    async def _on_auth_response(self, msg: WSMessage) -> None:
        data = msg.data if isinstance(msg.data, dict) else {}
        code = data.get("code", 0)

        if code != 0 and code <= 200:
            self.is_authenticated = True
            log.info("Authentication successful")

            client_info = WSMessage(ACTION_CLIENT_INFO, {
                "name": "FastNodeSync-CLI",
                "version": "0.1.0",
                "type": "cli",
            })
            await self._raw_send(client_info.encode())
            await self._flush_queue()
            self._ready_event.set()
            if self._connect_count > 1 and self._on_reconnect:
                if self._reconnect_task and not self._reconnect_task.done():
                    self._reconnect_task.cancel()
                self._reconnect_task = asyncio.create_task(self._run_reconnect_handler())
        else:
            err = data.get("msg", data.get("message", "unknown"))
            log.error("Authentication failed (code=%s): %s", code, err)
            self._running = False
            if self.ws:
                await self.ws.close()

    async def _run_reconnect_handler(self) -> None:
        try:
            assert self._on_reconnect is not None
            await self._on_reconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reconnect handler error")

    async def wait_ready(self, timeout: float = 30) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self._running = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self.ws:
            await self.ws.close()
