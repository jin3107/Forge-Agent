"""Do not invent a subprocess exit code when wait fails."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import background, tools


def task(proc, killed=False):
    return {'id':'bg-wait','cmd':'fixture','proc':proc,'buf':bytearray(),'off':0,'base_offset':0,
            'status':'running','code':None,'started':time.time(),'killed':killed,
            'scope':background._scope(Path('/tmp'),owner_id='alice',session_id='s')}

def proc_with_wait(wait):
    return SimpleNamespace(pid=987654, returncode=-9,
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[b'captured',b''])), wait=wait,
        kill=lambda:None)

class BackgroundWaitFailure(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'posix', 'test uses a POSIX process-group mock')
    async def test_wait_exception_keeps_unknown_task_and_no_fake_code(self):
        proc = proc_with_wait(AsyncMock(side_effect=[OSError('PRIVATE_WAIT_ERROR'), -9]))
        t = task(proc)
        await background._pump(t)
        self.assertEqual(t['status'], 'running', 'uncertain tasks remain eligible for cleanup/admission accounting')
        self.assertIsNone(t['code'], 'do not invent a process exit code')
        detail = background.render(t, True)
        self.assertIn('exit status could not be confirmed', detail.lower())
        self.assertIn('captured', detail)
        self.assertNotIn('PRIVATE_WAIT_ERROR', detail)
        background._TASKS[t['id']] = t
        try:
            with patch.object(background, 'MAX_TOTAL', 0):
                background._reap()
            self.assertIn(t['id'], background._TASKS, 'uncertain task must not be reaped as completed')
            self.assertIn('exit status could not be confirmed', background.summary(Path('/tmp'),owner_id='alice',session_id='s').lower())
            self.assertNotIn('bg-wait', background.summary(Path('/tmp'),owner_id='bob',session_id='s'))
            tool = await tools.execute_tool('check_background', {'id':'bg-wait'}, Path('/tmp'), owner_id='alice', session_id='s')
            self.assertTrue(tool['ok'], 'successful check does not confirm process outcome')
            self.assertIn('exit status could not be confirmed', tool['output'].lower())
            with patch('engine.background.os.killpg'):
                result, uncertain = await background._kill_task(t)
            self.assertFalse(uncertain, result)
            self.assertEqual(t['status'], 'killed')
            self.assertEqual(t['code'], -9)
            self.assertNotIn('exit status could not be confirmed', background.render(t, True).lower())
        finally:
            background._TASKS.pop(t['id'], None)

    async def test_wait_failure_does_not_override_an_independently_confirmed_kill(self):
        t = task(proc_with_wait(AsyncMock(side_effect=OSError('error'))), killed=True)
        t['status'] = 'killed'
        t['code'] = -9
        await background._pump(t)
        self.assertEqual(t['status'], 'killed')
        self.assertEqual(t['code'], -9)
        self.assertNotIn('exit status could not be confirmed', background.render(t, True).lower())

    async def test_real_subprocess_with_injected_wait_failure(self):
        with tempfile.TemporaryDirectory(prefix='forge-wait-error-') as td:
            tid, message = await background.start('wait fixture', Path(td),
                exec_args=[sys.executable,'-u','-c',"print('REAL_WAIT_OUTPUT')"])
            self.assertTrue(tid, message)
            t = background.get(tid)
            original_wait = t['proc'].wait
            injected = asyncio.Event()
            async def broken_wait():
                await original_wait()
                injected.set()
                raise OSError('SECRET_WAIT_DETAIL')
            t['proc'].wait = broken_wait
            try:
                await asyncio.wait_for(injected.wait(), 3)
                await asyncio.sleep(0)
                self.assertEqual(t['status'], 'running')
                self.assertIsNone(t['code'])
                detail = background.render(t, True)
                self.assertIn('REAL_WAIT_OUTPUT', detail)
                self.assertIn('exit status could not be confirmed', detail.lower())
                self.assertNotIn('SECRET_WAIT_DETAIL', detail)
            finally:
                t['proc'].wait = original_wait
                if t['proc'].returncode is None:
                    t['proc'].kill()
                await asyncio.wait_for(original_wait(), 3)
                background._TASKS.pop(tid, None)

    async def test_wait_returning_no_code_is_unconfirmed(self):
        t = task(proc_with_wait(AsyncMock(return_value=None)))
        await background._pump(t)
        self.assertEqual(t['status'], 'running')
        self.assertIsNone(t['code'])
        self.assertIn('exit status could not be confirmed', background.render(t, True).lower())

    @unittest.skipUnless(os.name == 'posix', 'test uses a POSIX process-group mock')
    async def test_stop_wait_error_is_uncertain_not_fake_success(self):
        for wait in (AsyncMock(side_effect=OSError('PRIVATE_STOP_ERROR')), AsyncMock(return_value=None)):
            with self.subTest(wait=wait):
                t = task(proc_with_wait(wait))
                with patch('engine.background.os.killpg'):
                    result, uncertain = await background._kill_task(t)
                self.assertTrue(uncertain)
                self.assertTrue(result.startswith('Error'))
                self.assertNotIn('PRIVATE_STOP_ERROR', result)
                self.assertEqual(t['status'], 'running')
                self.assertIsNone(t['code'])
                self.assertIn('exit status could not be confirmed', background.render(t, True).lower())
                self.assertTrue(t['killed'], 'stop signal was already sent')

    async def test_successful_zero_and_nonzero_codes_remain_unchanged(self):
        for code, status in [(0,'done'),(42,'failed')]:
            with self.subTest(code=code):
                t=task(proc_with_wait(AsyncMock(return_value=code)))
                await background._pump(t)
                self.assertEqual(t['status'],status)
                self.assertEqual(t['code'],code)
                self.assertNotIn('exit status could not be confirmed', background.render(t,True).lower())
