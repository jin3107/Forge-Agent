"""Cancellation while background process creation is in progress."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import background


class BackgroundSpawnCancellation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-bg-spawn-cancel-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.old_tasks = background._TASKS.copy()
        background._TASKS.clear()
        background.resume_starts()

    async def asyncTearDown(self):
        background._TASKS.clear()
        background._TASKS.update(self.old_tasks)
        background.resume_starts()

    async def test_cancel_after_real_process_created_keeps_slot_and_cleans_it(self):
        original = asyncio.create_subprocess_exec
        created = asyncio.Event()
        release = asyncio.Event()
        processes = []
        async def delayed_return(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        task = None
        try:
            with patch.object(background, 'MAX_RUNNING', 1), \
                 patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed_return):
                task = asyncio.create_task(background.start('fixture', self.ws,
                    exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
                await asyncio.wait_for(created.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertIsNone(processes[0].returncode)
                rejected, _ = await background.start('should be blocked', self.ws)
                self.assertFalse(rejected, 'slot released before spawned process was cleaned')
                release.set()
                async def cleaned():
                    while background._STARTING:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(cleaned(), 3)
                self.assertIsNotNone(processes[0].returncode)
                self.assertEqual(background._STARTING, 0)
                tid, _ = await background.start('after cleanup', self.ws,
                    exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)'])
                self.assertTrue(tid)
                await background.kill(tid, self.ws)
        finally:
            release.set()
            for proc in processes:
                if proc.returncode is None:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 3)

    async def test_cancel_after_spawn_result_ready_still_kills_process(self):
        original_shield = asyncio.shield
        ready, release = asyncio.Event(), asyncio.Event()
        async def delayed_shield(task):
            result = await original_shield(task)
            ready.set()
            await release.wait()
            return result
        with patch('engine.background.asyncio.shield', side_effect=delayed_shield):
            start = asyncio.create_task(background.start('fixture', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
            await asyncio.wait_for(ready.wait(), 2)
            start.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await start
                async def cleaned():
                    while background._STARTING:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(cleaned(), 3)
                tasks = list(background._TASKS.values())
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]['status'], 'killed')
                self.assertIsNotNone(tasks[0]['proc'].returncode)
            finally:
                release.set()

    async def test_shutdown_tracks_cancelled_inflight_start(self):
        original = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        with patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed):
            start = asyncio.create_task(background.start('fixture', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
            try:
                await asyncio.wait_for(created.wait(), 2)
                start.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start
                shutdown = asyncio.create_task(background.shutdown())
                await asyncio.sleep(0.05)
                self.assertFalse(shutdown.done(), 'shutdown did not wait for in-flight cleanup')
                release.set()
                result = await asyncio.wait_for(shutdown, 4)
                self.assertEqual(result['pending_starts'], 0)
                self.assertEqual(result['failures'], 0)
                self.assertIsNotNone(processes[0].returncode)
            finally:
                release.set()
                for proc in processes:
                    if proc.returncode is None:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), 3)

    async def test_cleanup_failure_keeps_task_registered_and_reports(self):
        original = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        with patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed), \
             patch('engine.background._kill_task', return_value=('Error: simulated', True)):
            start = asyncio.create_task(background.start('fixture', self.ws,
                exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']))
            try:
                await asyncio.wait_for(created.wait(), 2)
                start.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start
                with self.assertLogs('engine.background', level='WARNING') as logs:
                    release.set()
                    async def cleaned():
                        while background._STARTING:
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(cleaned(), 3)
                self.assertIn('cleanup incomplete', ' '.join(logs.output))
                tasks = list(background._TASKS.values())
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]['status'], 'running')
            finally:
                release.set()
                for proc in processes:
                    if proc.returncode is None:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), 3)

    async def test_cancel_before_spawn_failure_reserves_then_releases(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_failure(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError('test spawn failure')
        with patch.object(background, 'MAX_RUNNING', 1), \
             patch('engine.background.asyncio.create_subprocess_exec', side_effect=delayed_failure):
            task = asyncio.create_task(background.start('fixture', self.ws, exec_args=['fixture']))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            try:
                rejected, _ = await background.start('blocked', self.ws)
                self.assertFalse(rejected)
            finally:
                release.set()
            async def cleaned():
                while background._STARTING:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(cleaned(), 3)
            self.assertEqual(background._STARTING, 0)
            self.assertFalse(background._TASKS)


if __name__ == '__main__':
    unittest.main()
