"""Background shutdown waits briefly for output pumps without cancelling them."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import background

class BackgroundShutdownPumps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-bg-pump-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.old_tasks = background._TASKS.copy()
        background._TASKS.clear()
        background.resume_starts()
        self.release = asyncio.Event()
        self.pump_entered = asyncio.Event()
        self.pump_finished = asyncio.Event()
        self.original_pump = background._pump
        async def gated_pump(t):
            self.pump_entered.set()
            try:
                await self.release.wait()
                await self.original_pump(t)
            finally:
                self.pump_finished.set()
        self.patch_pump = patch.object(background, '_pump', gated_pump)
        self.patch_pump.start()
        self.addCleanup(self.patch_pump.stop)
        self.tid = None

    async def asyncTearDown(self):
        self.release.set()
        if self.tid:
            t = background.get(self.tid)
            if t and t['proc'].returncode is None:
                try: t['proc'].kill()
                except ProcessLookupError: pass
                await asyncio.wait_for(t['proc'].wait(), 3)
            if self.pump_entered.is_set():
                await asyncio.wait_for(self.pump_finished.wait(), 3)
        background._TASKS.clear()
        background._TASKS.update(self.old_tasks)
        background.resume_starts()

    async def spawn(self):
        self.tid, message = await background.start('fixture', self.ws,
            exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)'])
        self.assertTrue(self.tid, message)
        await asyncio.wait_for(self.pump_entered.wait(), 2)
        return background.get(self.tid)

    async def test_shutdown_waits_for_pending_pump(self):
        item = await self.spawn()
        with patch.object(background, 'SHUTDOWN_PUMP_WAIT', 2.0, create=True):
            stopping = asyncio.create_task(background.shutdown())
            async def killed():
                while item['proc'].returncode is None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(killed(), 2)
            await asyncio.sleep(0.05)
            self.assertFalse(stopping.done(), 'shutdown must wait for an outstanding output pump')
            self.release.set()
            result = await asyncio.wait_for(stopping, 3)
        self.assertEqual(result['pending_pumps'], 0)
        self.assertTrue(self.pump_finished.is_set())
        self.assertTrue(item['proc'].stdout.at_eof())

    async def test_cancelling_shutdown_does_not_cancel_output_pump(self):
        item = await self.spawn()
        with patch.object(background, 'SHUTDOWN_PUMP_WAIT', 2.0, create=True):
            stopping = asyncio.create_task(background.shutdown())
            async def killed():
                while item['proc'].returncode is None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(killed(), 2)
            await asyncio.sleep(0.05)
            self.assertFalse(stopping.done())
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
        self.assertFalse(self.pump_finished.is_set())
        self.release.set()
        await asyncio.wait_for(self.pump_finished.wait(), 2)
        self.assertTrue(item['proc'].stdout.at_eof())

    async def test_lifespan_logs_timed_out_pump(self):
        from server.main import app
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        exited = False
        try:
            await self.spawn()
            with patch.object(background, 'SHUTDOWN_PUMP_WAIT', 0.05, create=True):
                with self.assertLogs('server.main', level='WARNING') as logs:
                    await asyncio.wait_for(cm.__aexit__(None, None, None), 3)
                    exited = True
            self.assertIn('pending_pumps', ' '.join(logs.output))
            self.assertFalse(self.pump_finished.is_set())
            self.release.set()
            await asyncio.wait_for(self.pump_finished.wait(), 2)
        finally:
            self.release.set()
            if not exited:
                await cm.__aexit__(None, None, None)

    async def test_timeout_reports_pump_without_cancelling_even_if_task_reaped(self):
        item = await self.spawn()
        await background.kill(self.tid, self.ws)
        background._TASKS.pop(self.tid)
        with patch.object(background, 'SHUTDOWN_PUMP_WAIT', 0.05, create=True):
            result = await asyncio.wait_for(background.shutdown(), 2)
        self.assertEqual(result['pending_pumps'], 1)
        self.assertFalse(self.pump_finished.is_set(), 'timed out pump must not be cancelled')
        self.release.set()
        await asyncio.wait_for(self.pump_finished.wait(), 2)
        self.assertTrue(item['proc'].stdout.at_eof())
        result = await background.shutdown()
        self.assertEqual(result['pending_pumps'], 0)

@unittest.skipUnless(os.name == 'posix', 'requires POSIX sessions')
class EscapedPipeShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_escaped_descendant_holds_pipe_and_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='forge-bg-escaped-pipe-') as td:
            ws = Path(td)
            pidfile = ws / 'pid'
            old_tasks = background._TASKS.copy()
            background._TASKS.clear()
            background.resume_starts()
            tid = None
            pid = None
            code = ("import os,pathlib,subprocess,sys; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'], start_new_session=True); "
                    "f=pathlib.Path(sys.argv[1]); q=f.with_suffix('.part'); q.write_text(str(p.pid)); os.replace(q,f)")
            try:
                tid, message = await background.start('escaped-pipe fixture', ws,
                    exec_args=[sys.executable, '-u', '-c', code, str(pidfile)])
                self.assertTrue(tid, message)
                t = background.get(tid)
                async def ready():
                    while not pidfile.exists() or t['proc'].returncode is None:
                        await asyncio.sleep(0.01)
                    text = pidfile.read_text()
                    self.assertTrue(text.isascii() and text.isdecimal())
                    return int(text)
                pid = await asyncio.wait_for(ready(), 2)
                self.assertGreater(pid, 1)
                with patch.object(background, 'SHUTDOWN_PUMP_WAIT', 0.05):
                    result = await asyncio.wait_for(background.shutdown(), 2)
                self.assertEqual(result['pending_pumps'], 1)
                self.assertEqual(result['failures'], 1, 'missing process group cannot confirm group cleanup')
                self.assertFalse(t['killed'])
                self.assertIn('group stop could not be confirmed', background.render(t, True).lower())
                self.assertFalse(t['proc'].stdout.at_eof())
                os.killpg(pid, signal.SIGKILL)
                async def drained():
                    while not t['proc'].stdout.at_eof():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(drained(), 2)
                self.assertEqual((await background.shutdown())['pending_pumps'], 0)
            finally:
                if pid is None and pidfile.exists():
                    text = pidfile.read_text()
                    if text.isascii() and text.isdecimal() and int(text) > 1: pid = int(text)
                if pid is not None:
                    try: os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                if tid:
                    t = background.get(tid)
                    if t and t['proc'].returncode is None:
                        try: t['proc'].kill()
                        except ProcessLookupError: pass
                        await asyncio.wait_for(t['proc'].wait(), 3)
                background._TASKS.clear()
                background._TASKS.update(old_tasks)
                background.resume_starts()
