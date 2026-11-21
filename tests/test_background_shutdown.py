"""Graceful server shutdown of background tasks (not crash recovery)."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import background


class BackgroundShutdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-bg-shutdown-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.old_tasks = background._TASKS.copy()
        background._TASKS.clear()
        self.tids = []
        if hasattr(background, 'resume_starts'):
            background.resume_starts()

    async def asyncTearDown(self):
        for tid in self.tids:
            t = background.get(tid)
            if t and t['proc'].returncode is None:
                t['proc'].kill()
                try:
                    await asyncio.wait_for(t['proc'].wait(), 2)
                except asyncio.TimeoutError:
                    pass
        background._TASKS.clear()
        background._TASKS.update(self.old_tasks)
        if hasattr(background, 'resume_starts'):
            background.resume_starts()
        self.tmp.cleanup()

    async def spawn(self, owner='owner'):
        tid, message = await background.start('shutdown fixture', self.ws, owner_id=owner,
            exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)'])
        self.assertTrue(tid, message)
        self.tids.append(tid)
        return tid

    async def test_asgi_lifespan_stops_registered_tasks_from_multiple_scopes(self):
        from server.main import app
        async with app.router.lifespan_context(app):
            first = await self.spawn('alice')
            second = await self.spawn('bob')
            first_proc = background.get(first)['proc']
            second_proc = background.get(second)['proc']
        self.assertIsNotNone(first_proc.returncode)
        self.assertIsNotNone(second_proc.returncode)
        self.assertEqual(background.get(first)['status'], 'killed')
        self.assertEqual(background.get(second)['status'], 'killed')
        tid, message = await background.start('late', self.ws)
        self.assertFalse(tid)
        self.assertTrue(message.startswith('Error'))

    @unittest.skipUnless(os.name == 'posix', 'process groups are POSIX-specific')
    async def test_lifespan_stops_same_group_descendant(self):
        from server.main import app
        pidfile, marker = self.ws / 'pid', self.ws / 'survived'
        parent_code = ("import os,pathlib,subprocess,sys,time; "
            "c=subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(1.5); pathlib.Path(sys.argv[1]).write_text(\\\"alive\\\")',sys.argv[2]]); "
            "p=pathlib.Path(sys.argv[1]); tmp=p.with_suffix('.part'); tmp.write_text(str(c.pid)); os.replace(tmp,p); time.sleep(30)")
        group_id = None
        try:
            async with app.router.lifespan_context(app):
                tid, _ = await background.start('tree fixture', self.ws, exec_args=[
                    sys.executable, '-u', '-c', parent_code, str(pidfile), str(marker)])
                self.assertTrue(tid)
                self.tids.append(tid)
                group_id = background.get(tid)['proc'].pid
                async def ready():
                    while not pidfile.exists():
                        await asyncio.sleep(0.01)
                    value = pidfile.read_text()
                    self.assertTrue(value.isascii() and value.isdecimal())
                    self.assertGreater(int(value), 1)
                await asyncio.wait_for(ready(), 3)
            await asyncio.sleep(1.7)
            self.assertFalse(marker.exists(), 'descendant survived graceful shutdown')
        finally:
            if group_id is not None:
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_inflight_spawn_is_cleaned_when_shutdown_begins(self):
        original = asyncio.create_subprocess_exec
        entered, release = asyncio.Event(), asyncio.Event()
        created = []
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            proc = await original(*args, **kwargs)
            created.append(proc)
            return proc
        with patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed):
            start = asyncio.create_task(background.start('inflight', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
            await asyncio.wait_for(entered.wait(), 2)
            shutdown = asyncio.create_task(background.shutdown())
            await asyncio.sleep(0)
            late, msg = await background.start('late', self.ws)
            self.assertFalse(late)
            self.assertTrue(msg.startswith('Error'))
            release.set()
            result = await asyncio.wait_for(start, 5)
            await asyncio.wait_for(shutdown, 6)
        self.assertFalse(result[0], result)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    async def test_timed_out_inflight_start_still_stops_after_restart(self):
        original = asyncio.create_subprocess_exec
        entered, release = asyncio.Event(), asyncio.Event()
        created = []
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            proc = await original(*args, **kwargs)
            created.append(proc)
            return proc
        with patch.object(background, 'SHUTDOWN_START_WAIT', 0.05), \
             patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed):
            start = asyncio.create_task(background.start('inflight', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
            await asyncio.wait_for(entered.wait(), 2)
            try:
                result = await asyncio.wait_for(background.shutdown(), 1)
                self.assertEqual(result['pending_starts'], 1)
                background.resume_starts()
            finally:
                release.set()
            start_result = await asyncio.wait_for(start, 5)
        self.assertFalse(start_result[0])
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    async def test_shutdown_reports_stop_failure_without_exposing_output(self):
        tid = await self.spawn()
        with patch('engine.background._kill_task', return_value=('Error: simulated', True)):
            result = await background.shutdown()
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['failures'], 1)
        self.assertNotIn(tid, str(result))

    async def test_restart_reopens_admission(self):
        await background.shutdown()
        tid, _ = await background.start('late', self.ws)
        self.assertFalse(tid)
        background.resume_starts()
        await self.spawn()


if __name__ == '__main__':
    unittest.main()
