"""UTF-8 finalization follows output reading, not leader process status."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine import background


def task(proc):
    return {'id':'bg-utf8', 'cmd':'fixture', 'proc':proc, 'buf':bytearray(), 'off':0,
            'base_offset':0, 'status':'running', 'code':None, 'started':time.time(),
            'killed':False, 'scope':background._scope(Path('/tmp'))}


class OutputEndUTF8(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_leader_exit_does_not_finalize_live_pipe(self):
        release = asyncio.Event()
        ready = asyncio.Event()
        reads = 0
        async def read(_):
            nonlocal reads
            reads += 1
            if reads == 1:
                return b'\xe1\xbb'
            if reads == 2:
                ready.set()
                await release.wait()
                return b'\x87END'
            return b''
        t = task(SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock(return_value=-9)))
        pump = asyncio.create_task(background._pump(t))
        try:
            await asyncio.wait_for(ready.wait(), 1)
            t.update(status='killed', code=-9, killed=True)
            first = background.render(t, True)
            self.assertNotIn('�', first)
            self.assertEqual(t['off'], 0)
            release.set()
            await asyncio.wait_for(pump, 1)
            second = background.render(t, True)
            self.assertIn('ệEND', second)
            self.assertNotIn('�', second)
        finally:
            release.set()
            await asyncio.gather(pump, return_exceptions=True)

    async def test_eof_finalizes_incomplete_bytes_even_if_wait_fails(self):
        proc = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b'\xe1\xbb', b''])),
                               wait=AsyncMock(side_effect=OSError('wait failure')))
        t = task(proc)
        await background._pump(t)
        self.assertEqual(t['status'], 'running')
        result = background.render(t, True)
        self.assertIn('�', result)
        self.assertEqual(t['off'], 2)
        self.assertNotIn('remaining', result)

    async def test_real_child_truncated_utf8_is_finalized_at_eof(self):
        with tempfile.TemporaryDirectory() as td:
            tid, message = await background.start('utf8 fixture', Path(td), exec_args=[sys.executable, '-c', "import os; os.write(1, bytes([225, 187]))"])
            self.assertTrue(tid, message)
            t = background.get(tid)
            try:
                async def finished():
                    while t['status'] == 'running':
                        await asyncio.sleep(.01)
                await asyncio.wait_for(finished(), 3)
                self.assertEqual(t['code'], 0)
                result = background.render(t, True)
                self.assertIn('�', result)
                self.assertEqual(t['off'], 2)
            finally:
                if t['proc'].returncode is None:
                    t['proc'].kill()
                await asyncio.wait_for(t['proc'].wait(), 3)
                background._TASKS.pop(tid, None)

    async def test_read_failure_and_cancel_finalize_incomplete_bytes(self):
        for kind in ('failure', 'cancel'):
            with self.subTest(kind=kind):
                entered = asyncio.Event()
                reads = 0
                async def read(_):
                    nonlocal reads
                    reads += 1
                    if reads == 1:
                        return b'\xe1\xbb'
                    entered.set()
                    if kind == 'failure':
                        raise OSError('private')
                    await asyncio.Event().wait()
                t = task(SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock(return_value=0)))
                pump = asyncio.create_task(background._pump(t))
                await asyncio.wait_for(entered.wait(), 1)
                if kind == 'cancel':
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
                else:
                    await asyncio.wait_for(pump, 1)
                result = background.render(t, True)
                self.assertIn('�', result)
                self.assertIn('output may be incomplete', result)
                self.assertEqual(t['off'], 2)
                self.assertNotIn('remaining', result)
