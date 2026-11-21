"""Per-process/event-loop foreground command admission regressions."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import tools


def fake_process():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout.read = AsyncMock(side_effect=[b'ok', b''])
    proc.wait = AsyncMock(return_value=0)
    return proc


class ForegroundAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-fg-admission-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def test_simultaneous_spawns_respect_limit(self):
        entered = 0
        first, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            nonlocal entered
            entered += 1
            first.set()
            await release.wait()
            return fake_process()
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 2, create=True), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=spawn):
            tasks = [asyncio.create_task(tools._run_command_result(self.ws, {'command': f'test-{i}'}))
                     for i in range(10)]
            try:
                await asyncio.wait_for(first.wait(), 2)
                await asyncio.sleep(0.05)
            finally:
                release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        accepted = [r for r in results if r['ok']]
        blocked = [r for r in results if r['blocked']]
        self.assertEqual(len(accepted), 2, results)
        self.assertEqual(len(blocked), 8, results)
        self.assertEqual(entered, 2)
        for result in blocked:
            self.assertIsNone(result['exit_code'])
            self.assertFalse(result['timed_out'])
            self.assertFalse(result['truncated'])

    async def check_late_spawn_holds_slot(self, timeout):
        original = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            created.set()
            await release.wait()
            return proc
        argv = [sys.executable, '-u', '-c', 'import time; time.sleep(30)']
        task = None
        try:
            with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1, create=True), \
                 patch('engine.tools._prepare_command', return_value=(None, argv)), \
                 patch('engine.tools.asyncio.create_subprocess_exec', side_effect=delayed):
                task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first', 'timeout_sec': 1 if timeout else 30}))
                await asyncio.wait_for(created.wait(), 2)
                if timeout:
                    result = await asyncio.wait_for(task, 2)
                    self.assertTrue(result['timed_out'], result)
                else:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertIsNone(processes[0].returncode)
                with patch('engine.tools.asyncio.create_subprocess_exec', new_callable=AsyncMock,
                           side_effect=OSError('should not spawn')):
                    second = await tools._run_command_result(self.ws, {'command': 'second'})
                self.assertTrue(second['blocked'], second)
                release.set()
                async def settled():
                    while tools._FOREGROUND_START_CLEANUPS:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(settled(), 3)
                self.assertIsNotNone(processes[0].returncode)
                with patch('engine.tools.asyncio.create_subprocess_exec', new_callable=AsyncMock,
                           return_value=fake_process()):
                    third = await tools._run_command_result(self.ws, {'command': 'third'})
                self.assertTrue(third['ok'], third)
        finally:
            release.set()
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for proc in processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(proc.wait(), 3)
            async def settled():
                while tools._FOREGROUND_START_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 3)

    async def test_cancelled_spawn_retains_slot_until_cleanup(self):
        await self.check_late_spawn_holds_slot(False)

    async def test_timed_out_spawn_retains_slot_until_cleanup(self):
        await self.check_late_spawn_holds_slot(True)

    async def test_cancelled_spawn_failure_releases_slot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError('late failure')
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1, create=True), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=spawn):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first'}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                blocked = await tools._run_command_result(self.ws, {'command': 'second'})
                self.assertTrue(blocked['blocked'], blocked)
            finally:
                release.set()
            async def settled():
                while tools._FOREGROUND_START_CLEANUPS:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
            with patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                       return_value=fake_process()):
                result = await tools._run_command_result(self.ws, {'command': 'third'})
            self.assertTrue(result['ok'], result)

    async def test_real_running_command_blocks_then_cancellation_releases(self):
        original = asyncio.create_subprocess_exec
        ready = self.ws / 'ready'
        processes = []
        first_argv = [sys.executable, '-u', '-c',
                      "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)", str(ready)]
        second_argv = [sys.executable, '-u', '-c', "print('after-cancel')"]
        async def spawn(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            return proc
        def prepare(cmd, workspace, override=False):
            return None, first_argv if cmd == 'first' else second_argv
        task = None
        try:
            with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1, create=True), \
                 patch('engine.tools._prepare_command', side_effect=prepare), \
                 patch('engine.tools.asyncio.create_subprocess_exec', side_effect=spawn):
                task = asyncio.create_task(tools._run_command_result(self.ws, {'command': 'first'}))
                async def await_ready():
                    while not ready.exists():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(await_ready(), 3)
                blocked = await tools._run_command_result(self.ws, {'command': 'second'})
                self.assertTrue(blocked['blocked'], blocked)
                self.assertEqual(len(processes), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                next_result = await tools._run_command_result(self.ws, {'command': 'second'})
                self.assertTrue(next_result['ok'], next_result)
                self.assertIn('after-cancel', next_result['output'])
        finally:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for proc in processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(proc.wait(), 3)

    async def test_limit_metadata_propagates_through_tool_and_terminal(self):
        from server.main import ExecBody, workspace_exec
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1, create=True), \
             patch.object(tools, '_FOREGROUND_INFLIGHT', 1, create=True), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock), \
             patch('server.main._ws', return_value=self.ws):
            result = await tools.execute_tool('run_command', {'command': 'echo fixture'}, self.ws)
            terminal = await workspace_exec('session', ExecBody(command='echo fixture'), user=None)
        for value in (result, terminal):
            self.assertFalse(value['ok'])
            self.assertTrue(value['blocked'])
            self.assertIsNone(value['exit_code'])
            self.assertIn('limit', value['output'])

    async def test_error_and_policy_block_release_slot(self):
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1, create=True), \
             patch('engine.tools._prepare_command', side_effect=[('BLOCKED fixture', None), (None, None), (None, None)]), \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   side_effect=[OSError('spawn failure'), fake_process()]):
            first = await tools._run_command_result(self.ws, {'command': 'first'})
            second = await tools._run_command_result(self.ws, {'command': 'second'})
            third = await tools._run_command_result(self.ws, {'command': 'third'})
        self.assertTrue(first['blocked'])
        self.assertFalse(second['ok'])
        self.assertIn('spawn failure', second['output'])
        self.assertTrue(third['ok'], third)


if __name__ == '__main__':
    unittest.main()
