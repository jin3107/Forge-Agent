"""Retry a stop wait without signalling the same PID or group twice."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from engine import background


def task(proc):
    return {'id':'bg-retry', 'cmd':'fixture', 'proc':proc, 'buf':bytearray(), 'off':0,
            'base_offset':0, 'status':'running', 'code':None, 'started':time.time(),
            'killed':False, 'scope':background._scope(Path('/tmp'))}


@unittest.skipUnless(os.name == 'posix', 'POSIX signalling mock')
class BackgroundStopRetry(unittest.IsolatedAsyncioTestCase):
    async def test_failed_wait_can_retry_without_resignal(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.wait = AsyncMock(side_effect=[OSError('secret'), -9])
        t = task(proc)
        with patch('engine.background.os.killpg') as signal_group:
            _, uncertain = await background._kill_task(t)
            self.assertTrue(uncertain)
            result, uncertain = await background._kill_task(t)
        self.assertFalse(uncertain, result)
        signal_group.assert_called_once()
        self.assertEqual((t['status'], t['code']), ('killed', -9))
        self.assertFalse(t['wait_failed'])
        self.assertFalse(t['stop_unconfirmed'])

    async def test_timeout_retry_failure_stays_uncertain(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        t = task(proc)
        with patch('engine.background.os.killpg') as signal_group:
            for _ in range(2):
                result, uncertain = await background._kill_task(t)
                self.assertTrue(uncertain)
                self.assertTrue(result.startswith('Error'))
        signal_group.assert_called_once()
        self.assertEqual(t['status'], 'running')
        self.assertIsNone(t['code'])
        self.assertTrue(t['stop_unconfirmed'])
        self.assertTrue(t['wait_failed'])

    async def test_real_child_after_injected_wait_failure(self):
        with tempfile.TemporaryDirectory() as td:
            before = set(background._PUMPS)
            tid, message = await background.start('retry fixture', Path(td), exec_args=[sys.executable, '-u', '-c', "import time; print('READY', flush=True); time.sleep(30)"])
            self.assertTrue(tid, message)
            t = background.get(tid)
            pump = next(iter(background._PUMPS - before))
            original_wait = t['proc'].wait
            try:
                async def ready():
                    while b'READY' not in t['buf']:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(ready(), 3)
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                calls = 0
                async def flaky_wait():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError('private')
                    return await original_wait()
                t['proc'].wait = flaky_wait
                _, uncertain = await background._kill_task(t)
                self.assertTrue(uncertain)
                _, uncertain = await background._kill_task(t)
                self.assertFalse(uncertain)
                self.assertEqual(t['status'], 'killed')
                self.assertEqual(t['code'], -9)
                self.assertTrue(t['output_read_failed'])
            finally:
                t['proc'].wait = original_wait
                if t['proc'].returncode is None:
                    t['proc'].kill()
                await asyncio.wait_for(original_wait(), 3)
                background._TASKS.pop(tid, None)

    async def test_direct_fallback_retry_preserves_group_uncertainty(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.wait = AsyncMock(side_effect=[OSError('secret'), -9])
        t = task(proc)
        with patch('engine.background.os.killpg', side_effect=ProcessLookupError()) as signal_group:
            _, uncertain = await background._kill_task(t)
            self.assertTrue(uncertain)
            result, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain)
        signal_group.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual((t['status'], t['code']), ('killed', -9))
        self.assertTrue(t['stop_unconfirmed'])
        self.assertIn('group stop could not be confirmed', result)
        _, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain, 'finished leader does not confirm group cleanup')

    async def test_cancelled_stop_can_retry_without_resignal(self):
        entered = asyncio.Event()
        calls = 0
        async def wait():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return -9
        proc = MagicMock(pid=123456, returncode=None)
        proc.wait = wait
        t = task(proc)
        with patch('engine.background.os.killpg') as signal_group:
            stopper = asyncio.create_task(background._kill_task(t))
            await asyncio.wait_for(entered.wait(), 1)
            stopper.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopper
            self.assertTrue(t['stop_unconfirmed'])
            _, uncertain = await background._kill_task(t)
        self.assertFalse(uncertain)
        signal_group.assert_called_once()
        self.assertEqual(t['code'], -9)

    async def test_late_failed_wait_does_not_overwrite_confirmed_retry(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        async def wait():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise OSError('private')
            return -9
        proc = MagicMock(pid=123456, returncode=None)
        proc.wait = wait
        t = task(proc)
        with patch('engine.background.os.killpg') as signal_group:
            first = asyncio.create_task(background._kill_task(t))
            await asyncio.wait_for(entered.wait(), 1)
            _, retry_uncertain = await background._kill_task(t)
            release.set()
            _, first_uncertain = await first
        self.assertFalse(retry_uncertain)
        self.assertFalse(first_uncertain)
        self.assertFalse(t['stop_unconfirmed'])
        self.assertEqual((t['status'], t['code']), ('killed', -9))
        signal_group.assert_called_once()
