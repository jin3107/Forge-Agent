"""Foreground deadline spanning process creation and output collection."""
import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from engine import tools


class ForegroundDeadline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-fg-deadline-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def check_delayed_spawn(self, exec_path):
        name = 'create_subprocess_exec' if exec_path else 'create_subprocess_shell'
        original = getattr(asyncio, name)
        created, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        argv = [sys.executable, '-u', '-c', 'import time; time.sleep(30)']
        command = 'exec ' + ' '.join(shlex.quote(s) for s in argv) if os.name == 'posix' else subprocess.list2cmdline(argv)
        task = None
        try:
            with patch('engine.tools._prepare_command', return_value=(None, argv if exec_path else None)), \
                 patch(f'engine.tools.asyncio.{name}', side_effect=delayed):
                task = asyncio.create_task(tools._run_command_result(self.ws, {'command': command, 'timeout_sec': 1}))
                await asyncio.wait_for(created.wait(), 2)
                await asyncio.sleep(1.3)
                self.assertTrue(task.done(), 'deadline did not cover process creation')
                result = task.result()
                self.assertFalse(result['ok'])
                self.assertTrue(result['timed_out'], result)
                self.assertIsNone(result['exit_code'])
                self.assertFalse(result['truncated'])
                self.assertIn('timed out', result['output'])
                self.assertIn('Output unavailable', result['output'])
                self.assertIsNone(processes[0].returncode)
                release.set()
                async def settled():
                    while tools._FOREGROUND_START_CLEANUPS:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(settled(), 3)
                self.assertIsNotNone(processes[0].returncode)
        finally:
            release.set()
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # The application cleanup and this safety net may run concurrently.
            for proc in processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(proc.wait(), 3)
            async def wait_cleanup():
                while tools._FOREGROUND_START_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_cleanup(), 3)

    async def test_shell_creation_timeout_and_late_cleanup(self):
        await self.check_delayed_spawn(False)

    async def test_exec_creation_timeout_and_late_cleanup(self):
        await self.check_delayed_spawn(True)

    async def test_one_budget_is_shared_between_creation_and_execution(self):
        original = asyncio.create_subprocess_exec
        async def delayed(*args, **kwargs):
            await asyncio.sleep(0.6)
            return await original(*args, **kwargs)
        argv = [sys.executable, '-u', '-c', "import time; print('partial-marker', flush=True); time.sleep(0.8)"]
        with patch('engine.tools._prepare_command', return_value=(None, argv)), \
             patch('engine.tools.asyncio.create_subprocess_exec', side_effect=delayed):
            started = time.monotonic()
            result = await tools._run_command_result(self.ws, {'command': 'fixture', 'timeout_sec': 1})
            elapsed = time.monotonic() - started
        self.assertTrue(result['timed_out'], result)
        self.assertIn('partial-marker', result['output'])
        self.assertLess(elapsed, 2.5)

    async def test_late_spawn_error_after_deadline_is_consumed(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError('late spawn failure')
        with patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=delayed):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'fixture', 'timeout_sec': 1}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                result = await asyncio.wait_for(task, 2)
                self.assertTrue(result['timed_out'], result)
                self.assertTrue(tools._FOREGROUND_START_CLEANUPS)
            finally:
                release.set()
            async def settled():
                while tools._FOREGROUND_START_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
            self.assertFalse(tools._FOREGROUND_START_CLEANUPS)

    async def test_normal_command_within_shared_budget_succeeds(self):
        original = asyncio.create_subprocess_exec
        async def delayed(*args, **kwargs):
            await asyncio.sleep(0.1)
            return await original(*args, **kwargs)
        argv = [sys.executable, '-u', '-c', "import time; time.sleep(0.1); print('within-budget')"]
        with patch('engine.tools._prepare_command', return_value=(None, argv)), \
             patch('engine.tools.asyncio.create_subprocess_exec', side_effect=delayed):
            result = await tools._run_command_result(self.ws, {'command': 'fixture', 'timeout_sec': 1})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['timed_out'])
        self.assertIn('within-budget', result['output'])

    async def test_spawn_timeout_exception_is_reported_as_spawn_error(self):
        with patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   side_effect=TimeoutError('fixture spawn error')):
            result = await tools._run_command_result(self.ws, {'command': 'fixture', 'timeout_sec': 5})
        self.assertFalse(result['ok'])
        self.assertFalse(result['timed_out'], result)
        self.assertIn('fixture spawn error', result['output'])


if __name__ == '__main__':
    unittest.main()
