"""Cancelled background pumps must report incomplete observations honestly."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine import background, tools


def task(proc):
    return {'id': 'bg-cancel', 'cmd': 'fixture', 'proc': proc,
            'buf': bytearray(), 'off': 0, 'base_offset': 0,
            'status': 'running', 'code': None, 'started': time.time(), 'killed': False,
            'scope': background._scope(Path('/tmp'), 'alice', 's')}


class PumpCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_pump_starts_is_reported(self):
        proc = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock()), wait=AsyncMock())
        before = set(background._PUMPS)
        t = background._register_task('fixture', proc, background._scope(Path('/tmp'), 'alice', 's'))
        pump = next(iter(background._PUMPS - before))
        try:
            pump.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pump
            await asyncio.sleep(0)  # allow done callbacks to run
            self.assertTrue(t['output_read_failed'])
            self.assertTrue(t['wait_failed'])
            self.assertEqual(t['status'], 'running')
            proc.wait.assert_not_awaited()
        finally:
            background._TASKS.pop(t['id'], None)

    async def test_cancel_during_read_reports_both_uncertainties_without_waiting(self):
        entered = asyncio.Event()
        async def read(_):
            entered.set()
            await asyncio.Event().wait()
        proc = SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock())
        t = task(proc)
        background._TASKS[t['id']] = t
        pump = asyncio.create_task(background._pump(t))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            pump.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pump, 1)
            proc.wait.assert_not_awaited()
            self.assertEqual(t['status'], 'running')
            self.assertIsNone(t['code'])
            result = await tools.execute_tool('check_background', {'id':t['id']}, Path('/tmp'), owner_id='alice', session_id='s')
            self.assertIn('output may be incomplete', result['output'])
            self.assertIn('exit status could not be confirmed', result['output'])
            self.assertIn('output may be incomplete', background.summary(Path('/tmp'), owner_id='alice', session_id='s'))
            self.assertNotIn(t['id'], background.summary(Path('/tmp'), owner_id='bob', session_id='s'))
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            background._TASKS.pop(t['id'], None)

    async def test_cancel_during_wait_marks_exit_only(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        proc = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b'complete', b''])), wait=wait)
        t = task(proc)
        pump = asyncio.create_task(background._pump(t))
        await asyncio.wait_for(entered.wait(), 1)
        pump.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(pump, 1)
        self.assertIn('exit status could not be confirmed', background.render(t, True))
        self.assertFalse(t.get('output_read_failed', False))
        self.assertEqual(t['status'], 'running')

    async def test_cancel_does_not_override_independently_confirmed_exit(self):
        entered = asyncio.Event()
        async def read(_):
            entered.set()
            await asyncio.Event().wait()
        t = task(SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock()))
        pump = asyncio.create_task(background._pump(t))
        await asyncio.wait_for(entered.wait(), 1)
        t.update(status='killed', code=-9, killed=True)
        pump.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(pump, 1)
        self.assertEqual((t['status'], t['code']), ('killed', -9))
        self.assertFalse(t.get('wait_failed', False))
        self.assertTrue(t['output_read_failed'])

    @unittest.skipUnless(os.name == 'posix', 'real process group cleanup')
    async def test_real_child_cancelled_pump_remains_stoppable(self):
        with tempfile.TemporaryDirectory() as td:
            before = set(background._PUMPS)
            tid, message = await background.start('cancel fixture', Path(td), exec_args=[sys.executable, '-u', '-c', "import time; print('READY', flush=True); time.sleep(30)"])
            self.assertTrue(tid, message)
            t = background.get(tid)
            pump = next(iter(background._PUMPS - before))
            try:
                async def ready():
                    while b'READY' not in t['buf']:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(ready(), 3)
                pump.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(pump, 2)
                self.assertTrue(t['output_read_failed'])
                self.assertTrue(t['wait_failed'])
                result = await background.kill(tid, Path(td))
                self.assertEqual(t['status'], 'killed', result)
                self.assertIsInstance(t['code'], int)
                self.assertFalse(t['wait_failed'])
                self.assertIn('output may be incomplete', background.render(t, True))
            finally:
                if t['proc'].returncode is None:
                    t['proc'].kill()
                await asyncio.wait_for(t['proc'].wait(), 3)
                background._TASKS.pop(tid, None)
