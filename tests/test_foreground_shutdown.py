"""Graceful ASGI shutdown of already-tracked late foreground spawn cleanup."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from engine import background, tools


class ForegroundShutdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-fg-shutdown-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        background.resume_starts()

    async def asyncTearDown(self):
        background.resume_starts()

    async def prepare_late_process(self):
        original = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        patcher = patch('engine.tools.asyncio.create_subprocess_exec', side_effect=delayed)
        patcher.start()
        self.addCleanup(patcher.stop)
        argv = [sys.executable, '-u', '-c', 'import time; time.sleep(30)']
        with patch('engine.tools._prepare_command', return_value=(None, argv)):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'fixture'}))
            await asyncio.wait_for(created.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        return release, processes

    async def finish_cleanup(self, release, processes):
        release.set()
        async def settled():
            while tools._FOREGROUND_START_CLEANUPS:
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(settled(), 3)
        finally:
            for proc in processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(proc.wait(), 3)

    async def test_lifespan_waits_for_pending_foreground_and_stops_background_concurrently(self):
        from server.main import app
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        release, processes = None, []
        bg_tid = None
        exit_task = None
        try:
            bg_tid, _ = await background.start('bg fixture', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)'])
            bg_proc = background.get(bg_tid)['proc']
            release, processes = await self.prepare_late_process()
            exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
            async def bg_stopped():
                while bg_proc.returncode is None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(bg_stopped(), 2)
            self.assertFalse(exit_task.done(), 'lifespan did not wait for foreground cleanup')
            release.set()
            await asyncio.wait_for(exit_task, 3)
            self.assertIsNotNone(processes[0].returncode)
        finally:
            if release:
                await self.finish_cleanup(release, processes)
            if exit_task is None:
                await cm.__aexit__(None, None, None)
            elif not exit_task.done():
                await asyncio.wait_for(exit_task, 3)
            if bg_tid:
                t = background.get(bg_tid)
                if t and t['proc'].returncode is None:
                    t['proc'].kill()
                    await t['proc'].wait()
                background._TASKS.pop(bg_tid, None)

    async def test_lifespan_timeout_logs_without_cancelling_cleanup(self):
        from server.main import app
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        release, processes = None, []
        exited = False
        try:
            release, processes = await self.prepare_late_process()
            with patch.object(tools, 'FOREGROUND_CLEANUP_SHUTDOWN_WAIT', 0.05, create=True):
                with self.assertLogs('server.main', level='WARNING') as logs:
                    await asyncio.wait_for(cm.__aexit__(None, None, None), 2)
                    exited = True
            self.assertIn('Foreground', ' '.join(logs.output))
            self.assertTrue(tools._FOREGROUND_START_CLEANUPS)
            self.assertIsNone(processes[0].returncode)
            await self.finish_cleanup(release, processes)
            self.assertIsNotNone(processes[0].returncode)
        finally:
            if release:
                await self.finish_cleanup(release, processes)
            if not exited:
                await cm.__aexit__(None, None, None)

    async def test_background_failure_does_not_skip_foreground_wait(self):
        from server.main import app
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        release, processes = None, []
        exit_task = None
        try:
            release, processes = await self.prepare_late_process()
            with patch('server.main.background.shutdown', new_callable=AsyncMock,
                       side_effect=RuntimeError('fixture error')):
                with self.assertLogs('server.main', level='WARNING') as logs:
                    exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
                    await asyncio.sleep(0.05)
                    self.assertFalse(exit_task.done())
                    release.set()
                    await asyncio.wait_for(exit_task, 3)
            self.assertIn('Background shutdown failed', ' '.join(logs.output))
            self.assertIsNotNone(processes[0].returncode)
        finally:
            if release:
                await self.finish_cleanup(release, processes)
            if exit_task is None:
                await cm.__aexit__(None, None, None)
            elif not exit_task.done():
                await asyncio.wait_for(exit_task, 3)

    async def test_wait_helper_handles_multiple_and_does_not_cancel_pending(self):
        first, second = asyncio.Event(), asyncio.Event()
        a, b = asyncio.create_task(first.wait()), asyncio.create_task(second.wait())
        tools._FOREGROUND_START_CLEANUPS.update((a, b))
        try:
            first.set()
            await asyncio.wait_for(a, 2)
            with patch.object(tools, 'FOREGROUND_CLEANUP_SHUTDOWN_WAIT', 0.05, create=True):
                self.assertEqual(await tools.wait_foreground_start_cleanups(), 1)
            self.assertFalse(b.done())
            self.assertFalse(b.cancelled())
        finally:
            second.set()
            await asyncio.gather(a, b, return_exceptions=True)
            tools._FOREGROUND_START_CLEANUPS.difference_update((a, b))

    async def test_wait_helper_handles_empty_collection(self):
        self.assertEqual(await tools.wait_foreground_start_cleanups(), 0)


if __name__ == '__main__':
    unittest.main()
