"""Foreground cancellation while process creation is in flight."""
import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import tools


class ForegroundSpawnCancellation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-fg-spawn-cancel-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def run_cancelled_spawn(self, exec_path):
        original = asyncio.create_subprocess_exec if exec_path else asyncio.create_subprocess_shell
        method = 'create_subprocess_exec' if exec_path else 'create_subprocess_shell'
        spawned, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            spawned.set()
            await release.wait()
            return proc
        argv = [sys.executable, '-u', '-c', 'import time; time.sleep(30)']
        command = 'exec ' + ' '.join(shlex.quote(x) for x in argv) if os.name == 'posix' else ' '.join(argv)
        with patch('engine.tools._prepare_command', return_value=(None, argv if exec_path else None)), \
             patch(f'engine.tools.asyncio.{method}', side_effect=delayed):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': command}))
            try:
                await asyncio.wait_for(spawned.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertIsNone(processes[0].returncode)
                release.set()
                async def stopped():
                    while processes[0].returncode is None:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(stopped(), 2)
                async def settled():
                    while tools._FOREGROUND_START_CLEANUPS:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(settled(), 2)
                self.assertIsNotNone(processes[0].returncode)
            finally:
                release.set()
                for proc in processes:
                    if proc.returncode is None:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), 3)

    @unittest.skipUnless(os.name == 'posix', 'outside POSIX only the shell is stopped; the command would outlive the test')
    async def test_cancel_shell_spawn_cleans_created_process(self):
        await self.run_cancelled_spawn(False)

    async def test_cancel_exec_spawn_cleans_created_process(self):
        await self.run_cancelled_spawn(True)

    async def test_cancel_after_creation_result_ready(self):
        original_wait, original_spawn = asyncio.wait, asyncio.create_subprocess_exec
        ready, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def spawn(*args, **kwargs):
            proc = await original_spawn(*args, **kwargs)
            processes.append(proc)
            return proc
        async def delayed_wait(tasks, **kwargs):
            result = await original_wait(tasks, **kwargs)
            ready.set()
            await release.wait()
            return result
        argv = [sys.executable, '-u', '-c', 'import time; time.sleep(30)']
        with patch('engine.tools._prepare_command', return_value=(None, argv)), \
             patch('engine.tools.asyncio.create_subprocess_exec', side_effect=spawn), \
             patch('engine.tools.asyncio.wait', side_effect=delayed_wait):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'fixture'}))
            try:
                await asyncio.wait_for(ready.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                async def settled():
                    while tools._FOREGROUND_START_CLEANUPS:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(settled(), 3)
                self.assertIsNotNone(processes[0].returncode)
            finally:
                release.set()
                for proc in processes:
                    if proc.returncode is None:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), 3)

    async def test_spawn_failure_still_returns_error(self):
        with patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   side_effect=OSError('fixture failure')):
            result = await tools._run_command_result(self.ws, {'command': 'fixture'})
        self.assertFalse(result['ok'])
        self.assertIn('fixture failure', result['output'])

    @unittest.skipUnless(os.name == 'posix', 'group fallback is POSIX-specific')
    async def test_group_signal_failure_falls_back_and_is_reported(self):
        proc = MagicMock()
        proc.pid = 123456
        proc.stdout.read = AsyncMock(return_value=b'')
        proc.wait = AsyncMock(return_value=-9)
        async def spawn():
            return proc
        task = asyncio.create_task(spawn())
        with patch('engine.tools.os.killpg', side_effect=PermissionError('denied')):
            with self.assertLogs('engine.tools', level='WARNING') as logs:
                await tools._cleanup_cancelled_foreground_start(task)
        proc.kill.assert_called_once()
        self.assertIn('only signaled the direct process', ' '.join(logs.output))

    async def test_cancelled_delayed_spawn_failure_is_consumed(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError('fixture failure')
        with patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=spawn):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'fixture'}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            async def settled():
                while tools._FOREGROUND_START_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
            self.assertFalse(tools._FOREGROUND_START_CLEANUPS)


if __name__ == '__main__':
    unittest.main()
