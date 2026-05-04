"""Unit tests for CLI async runner cleanup."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest

if "yaml" not in sys.modules:
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda _stream: {}
    sys.modules["yaml"] = yaml_stub

if "click" not in sys.modules:
    click_stub = types.ModuleType("click")

    class _DummyGroup:
        def __init__(self, fn):
            self.fn = fn

        def command(self, *_args, **_kwargs):
            return _decorator

        def version_option(self, *_args, **_kwargs):
            return _decorator

        def __call__(self, *args, **kwargs):
            return self.fn(*args, **kwargs)

    def _decorator(*_args, **_kwargs):
        def wrap(fn):
            return fn

        return wrap

    def _group(*_args, **_kwargs):
        def wrap(fn):
            return _DummyGroup(fn)

        return wrap

    click_stub.group = _group
    click_stub.version_option = _decorator
    click_stub.option = _decorator
    click_stub.echo = lambda *_args, **_kwargs: None
    sys.modules["click"] = click_stub

from fns_cli.main import _run_async


class TestRunAsync(unittest.TestCase):

    def test_cancels_pending_tasks_before_closing_loop(self):
        cancelled = []

        async def runner():
            async def background():
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise

            asyncio.create_task(background())
            await asyncio.sleep(0)

        _run_async(runner())

        self.assertEqual(cancelled, [True])


if __name__ == "__main__":
    unittest.main()
