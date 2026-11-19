"""Checkpoint Git subprocess cancellation and timeout after creation."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import checkpoints, tools


class CheckpointProcessCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-checkpoint-child-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'child.py'
        self.ready = self.root / 'ready'
        self.marker = self.root / 'survived'
        self.script.write_text("import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(float(sys.argv[3]))\npathlib.Path(sys.argv[2]).write_text('survived')\n")
        self.processes = []
        self.orig_spawn = asyncio.create_subprocess_exec

    async def asyncTearDown(self):
        for p in self.processes:
            if p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(p.wait(), 3)

    async def spawn(self, *args, **kwargs):
        proc = await self.orig_spawn(*args, **kwargs)
        self.processes.append(proc)
        return proc

    async def ready_wait(self):
        while not self.ready.exists():
            await asyncio.sleep(0.01)

    def command(self, sleep=1.2):
        return [str(self.script), str(self.ready), str(self.marker), str(sleep)]

    async def test_cancellation_stops_and_reaps_real_child_for_both_helpers(self):
        for helper in ('_run', '_run_with_env'):
            with self.subTest(helper=helper):
                self.ready.unlink(missing_ok=True)
                self.marker.unlink(missing_ok=True)
                with patch.object(checkpoints, 'GIT', sys.executable), \
                     patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=self.spawn):
                    call = checkpoints._run(self.command()) if helper == '_run' else checkpoints._run_with_env(self.command(), dict(os.environ))
                    task = asyncio.create_task(call)
                    try:
                        await asyncio.wait_for(self.ready_wait(), 3)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        await asyncio.sleep(1.4)
                        self.assertFalse(self.marker.exists(), 'child continued after cancellation')
                        self.assertIsNotNone(self.processes[-1].returncode)
                    finally:
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        proc = self.processes[-1]
                        if proc.returncode is None:
                            proc.kill()
                            await asyncio.wait_for(proc.wait(), 3)

    async def test_timeout_stops_and_reaps_real_child(self):
        # The child has three seconds to start and report before the timeout (0.3 s was shorter
        # than interpreter start-up on a slow Windows machine) and would write its second file
        # half a second after the timeout if it were left running.
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch.object(checkpoints, 'GIT_COMMAND_TIMEOUT', 3.0, create=True), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=self.spawn):
            rc, out = await asyncio.wait_for(checkpoints._run_with_env(self.command(sleep=3.5), dict(os.environ)), 10)
        self.assertEqual((rc, out), (124, 'timeout'))
        self.assertTrue(self.ready.exists())
        self.assertIsNotNone(self.processes[-1].returncode)
        await asyncio.sleep(2.0)
        self.assertFalse(self.marker.exists())

    async def test_second_cancellation_does_not_cancel_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(_size):
            entered.set()
            await release.wait()
            return b''
        proc = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=read)
        proc.wait = AsyncMock(return_value=-9)
        proc.returncode = None
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=proc):
            task = asyncio.create_task(checkpoints._run([]))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(checkpoints._GIT_CLEANUPS), 1)
            release.set()
            async def settled():
                while checkpoints._GIT_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
            proc.kill.assert_called_once()

    async def test_foreground_cancellation_waits_for_real_checkpoint_child(self):
        # Replace the Git invocation with the same real delayed-marker child.
        async def spawn_git(*args, **kwargs):
            return await self.spawn(sys.executable, *self.command(), **kwargs)
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=spawn_git), \
             patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1):
            task = asyncio.create_task(tools.execute_tool('run_command', {'command': 'echo never'}, self.root))
            try:
                await asyncio.wait_for(self.ready_wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(self.processes[-1].returncode)
                self.assertEqual(tools._FOREGROUND_INFLIGHT, 0)
                await asyncio.sleep(1.3)
                self.assertFalse(self.marker.exists())
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def test_normal_output_and_nonzero_exit(self):
        with patch.object(checkpoints, 'GIT', sys.executable):
            rc, out = await checkpoints._run(['-c', "import sys; print('fixture'); sys.exit(7)"])
        self.assertEqual((rc, out), (7, 'fixture'))


if __name__ == '__main__':
    unittest.main()
