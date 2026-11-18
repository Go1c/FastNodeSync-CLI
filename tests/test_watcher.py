"""Unit tests for watchdog-to-asyncio debounce scheduling."""

from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fns_cli.watcher import _VaultEventHandler


class TestWatcherDebounce(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = MagicMock()
        engine.vault_path = Path("/vault")
        self.handler = _VaultEventHandler.__new__(_VaultEventHandler)
        self.handler.engine = engine
        self.handler.loop = asyncio.get_running_loop()
        self.handler._pending = {}
        self.handler._known_files = set()

    async def test_schedule_from_watchdog_thread_wakes_event_loop(self):
        fired = asyncio.Event()

        async def on_change():
            fired.set()

        with patch("fns_cli.watcher.DEBOUNCE_SECONDS", 0.01):
            thread = threading.Thread(
                target=self.handler._schedule,
                args=("mod:note.md", on_change),
            )
            thread.start()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            await asyncio.wait_for(fired.wait(), timeout=0.5)

    async def test_debounce_cancels_previous_timer_on_event_loop(self):
        calls = []
        both_scheduled = threading.Event()
        timer_threads = []
        timers = []

        def create_timer(delay, callback, *args):
            timer_threads.append(threading.get_ident())
            timer = asyncio.BaseEventLoop.call_later(
                self.handler.loop, delay, callback, *args
            )
            timers.append(timer)
            return timer

        async def on_change():
            calls.append("change")

        with patch("fns_cli.watcher.DEBOUNCE_SECONDS", 0.02), patch.object(
            self.handler.loop, "call_later", side_effect=create_timer
        ) as call_later:
            def schedule_twice():
                self.handler._schedule("mod:note.md", on_change)
                self.handler._schedule("mod:note.md", on_change)
                both_scheduled.set()

            thread = threading.Thread(target=schedule_twice)
            thread.start()
            thread.join(timeout=1)
            self.assertTrue(both_scheduled.wait(timeout=1))
            await asyncio.sleep(0.05)

        self.assertEqual(calls, ["change"])
        self.assertEqual(self.handler._pending, {})
        debounce_calls = [call for call in call_later.call_args_list if call.args[0] == 0.02]
        self.assertEqual(len(debounce_calls), 2)
        self.assertTrue(timers[0].cancelled())
        self.assertEqual(timer_threads[:2], [threading.get_ident()] * 2)


if __name__ == "__main__":
    unittest.main()
