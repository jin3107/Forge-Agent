"""Foreground admission retains ownership while checkpoint cleanup is detached."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import checkpoints, tools


class CheckpointForegroundLease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-ckpt-lease-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.release = asyncio.Event()
        self.processes = []
        self.orig_spawn = asyncio.create_subprocess_exec

    async def asyncTearDown(self):
        self.release.set()
        for proc in self.processes:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(proc.wait(), 3)
        async def wait():
            while checkpoints._GIT_CLEANUPS:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)

    async def assert_blocked_then_release(self):
        with patch('engine.checkpoints.snapshot', new_callable=AsyncMock,
                   side_effect=RuntimeError('unexpected snapshot')):
            try:
                blocked = await tools._run_command_result(self.ws, {'command': 'second'}, checkpoint=True)
            except RuntimeError as exc:
                self.fail(f'quota allowed a new snapshot: {exc}')
        self.assertTrue(blocked['blocked'], blocked)
        self.release.set()
        async def settled():
            while checkpoints._GIT_CLEANUPS:
                await asyncio.sleep(.01)
            await asyncio.sleep(0)
        await asyncio.wait_for(settled(), 3)
        with patch('engine.checkpoints.snapshot', new_callable=AsyncMock), \
             patch('engine.tools._prepare_command', return_value=('BLOCKED: policy fixture', None)):
            result = await tools._run_command_result(self.ws, {'command': 'third'}, checkpoint=True)
        self.assertEqual(result['output'], 'BLOCKED: policy fixture')
        self.assertEqual(tools._FOREGROUND_INFLIGHT, 0)

    async def test_cancelled_late_real_spawn_retains_slot(self):
        ready = self.ws / 'ready'
        marker = self.ws / 'survived'
        entered = asyncio.Event()
        code = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(1.5); pathlib.Path(sys.argv[2]).write_text('survived')"
        async def delayed(*args, **kwargs):
            proc = await self.orig_spawn(sys.executable, '-c', code, str(ready), str(marker), **kwargs)
            self.processes.append(proc)
            entered.set()
            await self.release.wait()
            return proc
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=delayed):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first'}, checkpoint=True))
            await asyncio.wait_for(entered.wait(), 3)
            async def wait_ready():
                while not ready.exists():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_ready(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_blocked_then_release()
            self.assertIsNotNone(self.processes[0].returncode)
            await asyncio.sleep(1.6)
            self.assertFalse(marker.exists())

    async def test_cancelled_late_spawn_failure_retains_then_releases(self):
        entered = asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await self.release.wait()
            raise OSError('late spawn failure')
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=delayed):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first'}, checkpoint=True))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_blocked_then_release()

    async def check_detached_cleanup(self, timeout):
        entered, killed = asyncio.Event(), asyncio.Event()
        proc = MagicMock()
        async def read(_size):
            entered.set()
            await self.release.wait()
            return b''
        proc.stdout.read = AsyncMock(side_effect=read)
        proc.wait = AsyncMock(return_value=-9)
        proc.kill.side_effect = killed.set
        proc.returncode = None
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch.object(checkpoints, 'GIT', sys.executable), \
             patch.object(checkpoints, 'GIT_COMMAND_TIMEOUT', .05 if timeout else 60), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=proc):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first'}, checkpoint=True))
            await asyncio.wait_for(entered.wait(), 3)
            if not timeout:
                task.cancel()
            await asyncio.wait_for(killed.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_blocked_then_release()
            proc.kill.assert_called_once()

    async def test_repeated_cancel_during_cleanup_retains_slot(self):
        await self.check_detached_cleanup(False)

    async def test_cancel_during_timeout_cleanup_retains_slot(self):
        await self.check_detached_cleanup(True)


if __name__ == '__main__':
    unittest.main()
