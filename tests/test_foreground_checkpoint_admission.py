"""Foreground slots cover checkpoint work as well as command execution."""
import asyncio
import os
import shlex
import subprocess
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


class ForegroundCheckpointAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-fg-checkpoint-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def test_full_limit_never_starts_checkpoint_or_command(self):
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch.object(tools, '_FOREGROUND_INFLIGHT', 1), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snapshot, \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock) as spawn:
            result = await tools.execute_tool('run_command', {'command': 'echo fixture'}, self.ws)
        self.assertTrue(result['blocked'], result)
        self.assertFalse(result['ok'])
        snapshot.assert_not_awaited()
        spawn.assert_not_awaited()

    async def test_simultaneous_checkpoints_cannot_exceed_foreground_limit(self):
        started, release = asyncio.Event(), asyncio.Event()
        events = []
        async def snapshot(*args, **kwargs):
            events.append('snapshot')
            started.set()
            await release.wait()
        async def spawn(*args, **kwargs):
            events.append('spawn')
            return fake_process()
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 2), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.checkpoints.snapshot', side_effect=snapshot), \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=spawn):
            tasks = [asyncio.create_task(tools.execute_tool('run_command', {'command': f'fixture-{i}'}, self.ws))
                     for i in range(10)]
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.sleep(0.05)
                self.assertEqual(events, ['snapshot', 'snapshot'])
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(sum(bool(r['ok']) for r in results), 2)
        self.assertEqual(sum(bool(r['blocked']) for r in results), 8)
        self.assertEqual(events.count('snapshot'), 2)
        self.assertEqual(events.count('spawn'), 2)

    async def test_admitted_command_checkpoints_before_spawning(self):
        events = []
        async def snapshot(*args, **kwargs):
            events.append('snapshot')
        async def spawn(*args, **kwargs):
            events.append('spawn')
            return fake_process()
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.checkpoints.snapshot', side_effect=snapshot) as mock_snap, \
             patch('engine.tools.asyncio.create_subprocess_shell', side_effect=spawn):
            result = await tools.execute_tool('run_command', {'command': 'echo fixture'}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(events, ['snapshot', 'spawn'])
        self.assertEqual(mock_snap.await_args.args[1], self.ws)
        self.assertEqual(mock_snap.await_args.args[2], 'run_command: echo fixture')

    @unittest.skipUnless(os.name == 'posix', 'fixture command uses POSIX quoting')
    async def test_real_git_checkpoint_captures_state_before_command(self):
        from engine import checkpoints
        if not checkpoints.available():
            self.skipTest('git unavailable')
        repo = self.ws.parent / 'fixture-checkpoint.git'
        target = self.ws / 'state.txt'
        target.write_text('before')
        code = "from pathlib import Path; Path('state.txt').write_text('after')"
        command = 'exec ' + shlex.join([sys.executable, '-c', code])
        with patch('engine.checkpoints._repo_dir', return_value=repo), \
             patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            result = await tools.execute_tool('run_command', {'command': command, 'timeout_sec': 10}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(target.read_text(), 'after')
        stored = subprocess.run([checkpoints.GIT, '--git-dir', str(repo), 'show',
                                 'refs/heads/forge:state.txt'], capture_output=True, text=True, check=True)
        self.assertEqual(stored.stdout, 'before')

    async def test_cancellation_during_checkpoint_releases_slot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def snapshot(*args, **kwargs):
            entered.set()
            await release.wait()
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch('engine.checkpoints.snapshot', side_effect=snapshot), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   return_value=fake_process()) as spawn:
            task = asyncio.create_task(tools.execute_tool('run_command', {'command': 'first'}, self.ws))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                rejected = await asyncio.wait_for(
                    tools.execute_tool('run_command', {'command': 'other'}, self.ws), 0.5)
                self.assertTrue(rejected['blocked'], rejected)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            result = await tools.execute_tool('run_command', {'command': 'after'}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(spawn.await_count, 1)

    async def test_checkpoint_exception_releases_slot_without_spawning(self):
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch('engine.tools._prepare_command', return_value=(None, None)), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock,
                   side_effect=[OSError('checkpoint failed'), None]), \
             patch('engine.tools.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   return_value=fake_process()) as spawn:
            failed = await tools.execute_tool('run_command', {'command': 'first'}, self.ws)
            succeeded = await tools.execute_tool('run_command', {'command': 'second'}, self.ws)
        self.assertFalse(failed['ok'], failed)
        self.assertIn('checkpoint failed', failed['output'])
        self.assertTrue(succeeded['ok'], succeeded)
        self.assertEqual(spawn.await_count, 1)

    async def test_other_write_tool_still_snapshots(self):
        with patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snapshot:
            result = await tools.execute_tool('write_file', {'path': 'fixture.txt', 'content': 'hello'}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.ws / 'fixture.txt').read_text(), 'hello')
        snapshot.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
