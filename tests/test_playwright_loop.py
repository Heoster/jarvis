"""The Playwright engine must live on ONE event loop.

The bug this pins down: ``start()`` used to boot Playwright inside a throw-away
``asyncio.run()`` while every later call (``goto``, ``evaluate``, ``new_page``)
was awaited on the agent's own loop.  Playwright's async objects are bound to
the loop they were created on, so the mismatch surfaced as
``BrowserContext.new_page: 'NoneType' object has no attribute 'send'`` — the
engine was written off as broken and JARVIS degraded to Selenium, which cannot
read a hydrated YouTube page.

No Playwright install is needed to prove the fix: what matters is that the
engine owns a background loop and that everything — the sync ``start`` path and
the async ``goto``/``evaluate`` calls alike — runs on that same loop, whatever
loop the caller happens to be on.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from jarvis.tools.browser.harness import PlaywrightEngine


class _FakeHarness:
    """The only thing an engine needs from its harness is its config."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.screen_engine_process = None


@pytest.fixture()
def engine():
    created = PlaywrightEngine(_FakeHarness({"timeout": 5000}))
    yield created
    created.stop()


def _loop_id_sync(engine) -> int:
    """Run a probe through the engine's *sync* path (what ``start()`` uses)."""
    async def probe():
        return id(asyncio.get_running_loop())

    return engine._run(probe())


def _loop_id_async(engine) -> int:
    """Run a probe through the engine's *async* path (what the tools use)."""
    async def probe():
        return id(asyncio.get_running_loop())

    async def caller():
        return await engine._dispatch(probe())

    return asyncio.run(caller())


def test_the_engine_owns_one_background_loop(engine):
    loop = engine._ensure_loop()
    assert isinstance(loop, asyncio.AbstractEventLoop)
    assert engine._ensure_loop() is loop                      # idempotent
    assert engine._thread is not None and engine._thread.is_alive()


def test_sync_and_async_paths_land_on_the_same_loop(engine):
    loop = engine._ensure_loop()
    # two different caller loops: asyncio.run() makes a fresh one each time
    assert _loop_id_sync(engine) == id(loop)
    assert _loop_id_async(engine) == id(loop)
    assert _loop_id_async(engine) == id(loop)                 # a second, new caller loop
    assert id(loop) not in {_caller_loop_id(), _caller_loop_id()}


def _caller_loop_id() -> int:
    async def probe():
        return id(asyncio.get_running_loop())

    return asyncio.run(probe())


def test_actions_from_another_loop_stay_on_the_engine_loop(engine):
    loop = engine._ensure_loop()

    async def note(value):
        return value, id(asyncio.get_running_loop())

    async def main():
        caller = id(asyncio.get_running_loop())
        rows = [("caller", caller)]
        rows.append(await engine._dispatch(note("goto")))
        rows.append(await engine._dispatch(note("evaluate")))
        return rows

    rows = asyncio.run(main())
    assert [value for value, _loop in rows] == ["caller", "goto", "evaluate"]
    assert rows[0][1] != id(loop)                             # the caller's own loop
    assert {loop_id for _value, loop_id in rows[1:]} == {id(loop)}


def test_concurrent_calls_share_the_loop_without_deadlocking(engine):
    engine._ensure_loop()

    async def slow(index):
        await asyncio.sleep(0.05)
        return id(asyncio.get_running_loop()), index

    async def main():
        return await asyncio.gather(*(engine._dispatch(slow(index))
                                      for index in range(5)))

    results = asyncio.run(main())
    assert {loop for loop, _index in results} == {id(engine._loop)}
    assert [index for _loop, index in results] == [0, 1, 2, 3, 4]


def test_stop_closes_the_loop_and_joins_the_thread(engine):
    engine._ensure_loop()
    thread = engine._thread
    engine.stop()
    assert engine._loop is None and engine._page is None and engine._browser is None
    deadline = 5.0
    while thread.is_alive() and deadline > 0:
        time.sleep(0.05)
        deadline -= 0.05
    assert not thread.is_alive()
