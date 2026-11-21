"""Background output read failures are visible without exposing exception text."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from engine import background, tools

class BackgroundReadFailure(unittest.IsolatedAsyncioTestCase):
    async def test_read_failure_is_visible_while_running_and_after_success(self):
        done = asyncio.Event()
        secret = 'PRIVATE_EXCEPTION_DETAIL'
        async def wait():
            await done.wait()
            return 0
        t = {'id':'bg-error','cmd':'fixture','buf':bytearray(),'off':0,'base_offset':0,
             'status':'running','code':None,'started':time.time(),'killed':False,
             'scope':background._scope(Path('/tmp'), owner_id='alice', session_id='session')}
        t['proc'] = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b'partial output', OSError(secret)])), wait=wait)
        background._TASKS[t['id']] = t
        pump = asyncio.create_task(background._pump(t))
        try:
            async def failed():
                while t['proc'].stdout.read.await_count < 2: await asyncio.sleep(0)
                await asyncio.sleep(0)
            await asyncio.wait_for(failed(), 2)
            self.assertEqual(t['status'], 'running')
            first = background.render(t, True)
            self.assertIn('partial output', first)
            self.assertIn('output may be incomplete', first.lower())
            self.assertNotIn(secret, first)
            self.assertIn('output may be incomplete', background.render(t, True).lower(), 'warning must persist after output is consumed')
            listing = background.summary(Path('/tmp'), owner_id='alice', session_id='session')
            self.assertIn('output may be incomplete', listing.lower())
            self.assertNotIn('bg-error', background.summary(Path('/tmp'), owner_id='bob', session_id='session'))
            via_tool = await tools.execute_tool('check_background', {'id':'bg-error'}, Path('/tmp'), owner_id='alice', session_id='session')
            self.assertIn('output may be incomplete', via_tool['output'].lower())
            done.set()
            await asyncio.wait_for(pump, 2)
            self.assertEqual(t['code'], 0, 'preserve real process exit status')
            self.assertEqual(t['status'], 'done')
            self.assertIn('output may be incomplete', background.render(t, True).lower())
        finally:
            done.set()
            await asyncio.gather(pump, return_exceptions=True)
            background._TASKS.pop(t['id'], None)

    async def test_real_subprocess_with_injected_read_error(self):
        with tempfile.TemporaryDirectory(prefix='forge-bg-read-error-') as td:
            ws = Path(td)
            tid, message = await background.start('read error fixture', ws,
                exec_args=[sys.executable, '-u', '-c', "print('REAL_PREFIX', flush=True)"])
            self.assertTrue(tid, message)
            t = background.get(tid)
            stream = t['proc'].stdout
            reads = 0
            async def faulty_read(size):
                nonlocal reads
                reads += 1
                if reads == 1:
                    return await stream.read(size)
                raise OSError('private fixture error')
            t['proc'].stdout = SimpleNamespace(read=faulty_read)
            try:
                async def done():
                    while t['status'] == 'running': await asyncio.sleep(0.01)
                await asyncio.wait_for(done(), 3)
                self.assertEqual(t['code'], 0)
                result = background.render(t, True)
                self.assertIn('REAL_PREFIX', result)
                self.assertIn('output may be incomplete', result.lower())
                self.assertNotIn('private fixture error', result)
            finally:
                if t['proc'].returncode is None:
                    t['proc'].kill()
                    await asyncio.wait_for(t['proc'].wait(), 3)
                background._TASKS.pop(tid, None)

    async def test_read_failure_does_not_hide_killed_status_or_exit_code(self):
        t = {'id':'bg-killed','cmd':'fixture','buf':bytearray(),'off':0,'base_offset':0,
             'status':'running','code':None,'started':time.time(),'killed':True}
        t['proc'] = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=OSError('sensitive'))), wait=AsyncMock(return_value=-9))
        await background._pump(t)
        self.assertEqual(t['status'], 'killed')
        self.assertEqual(t['code'], -9)
        output = background.render(t, True)
        self.assertIn('output may be incomplete', output.lower())
        self.assertNotIn('sensitive', output)

    async def test_no_warning_for_normal_read(self):
        t = {'id':'bg-ok','cmd':'fixture','buf':bytearray(),'off':0,'base_offset':0,
             'status':'running','code':None,'started':time.time(),'killed':False}
        t['proc'] = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b'ok',b''])), wait=AsyncMock(return_value=0))
        await background._pump(t)
        self.assertNotIn('output may be incomplete', background.render(t, True).lower())
