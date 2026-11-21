"""Cancellation while a checkpoint subprocess is being created."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class CheckpointSpawnCancel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-git-spawn-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ready = self.root / 'ready'
        self.marker = self.root / 'survived'
        self.script = self.root / 'child.py'
        self.script.write_text("import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(1.2)\npathlib.Path(sys.argv[2]).write_text('survived')\n")
        self.original = asyncio.create_subprocess_exec
        self.processes = []
        self.release = asyncio.Event()

    async def asyncTearDown(self):
        self.release.set()
        for p in self.processes:
            if p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(p.wait(), 3)

    async def wait_ready(self):
        while not self.ready.exists():
            await asyncio.sleep(.01)

    async def wait_cleanup(self):
        while checkpoints._GIT_CLEANUPS:
            await asyncio.sleep(.01)

    async def test_late_real_spawn_is_killed_after_caller_cancel(self):
        entered = asyncio.Event()
        async def delayed(*args, **kwargs):
            proc = await self.original(*args, **kwargs)
            self.processes.append(proc)
            entered.set()
            await self.release.wait()
            return proc
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=delayed):
            task = asyncio.create_task(checkpoints._run([str(self.script), str(self.ready), str(self.marker)]))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await asyncio.wait_for(self.wait_ready(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertEqual(len(checkpoints._GIT_CLEANUPS), 1)
                self.release.set()
                await asyncio.wait_for(self.wait_cleanup(), 3)
                self.assertIsNotNone(self.processes[-1].returncode)
                await asyncio.sleep(1.3)
                self.assertFalse(self.marker.exists())
            finally:
                self.release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def test_delayed_spawn_failure_consumed(self):
        entered = asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await self.release.wait()
            raise OSError('late failure')
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=delayed):
            task = asyncio.create_task(checkpoints._run_with_env([], dict(os.environ)))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(checkpoints._GIT_CLEANUPS), 1)
            self.release.set()
            await asyncio.wait_for(self.wait_cleanup(), 3)

    async def test_cancel_when_spawn_result_becomes_ready(self):
        caller = None
        async def spawn_and_cancel(*args, **kwargs):
            proc = await self.original(*args, **kwargs)
            self.processes.append(proc)
            caller.cancel()
            return proc
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=spawn_and_cancel):
            caller = asyncio.create_task(checkpoints._run([str(self.script), str(self.ready), str(self.marker)]))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            await asyncio.wait_for(self.wait_cleanup(), 3)
            self.assertIsNotNone(self.processes[-1].returncode)
            await asyncio.sleep(1.3)
            self.assertFalse(self.marker.exists())

    async def test_spawn_failure_propagates(self):
        async def failure(*args, **kwargs):
            raise OSError('spawn failure')
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=failure):
            with self.assertRaisesRegex(OSError, 'spawn failure'):
                await checkpoints._run([])


if __name__ == '__main__':
    unittest.main()
