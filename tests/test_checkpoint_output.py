"""Bound checkpoint Git output while continuing to drain the subprocess pipe."""
import asyncio
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import checkpoints


class CheckpointOutput(unittest.IsolatedAsyncioTestCase):
    async def run_python(self, code):
        with patch.object(checkpoints, 'GIT', sys.executable):
            return await asyncio.wait_for(checkpoints._run(['-c', code]), 10)

    async def test_large_output_drained_but_not_reported_as_success(self):
        # Child writes over a pipe's capacity; blocking reads or output buffering
        # would either time out or allocate proportional to the 12 MB output.
        code = "import sys; sys.stdout.buffer.write(b'x'*12000000); sys.stdout.flush()"
        tracemalloc.start()
        try:
            rc, out = await self.run_python(code)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(rc, 125, (rc, out[:100]))
        self.assertIn('output', out.lower())
        self.assertLess(len(out), 1000)
        self.assertLess(peak, 4_000_000, f'parent Python allocation peak {peak}')

    async def test_at_cap_is_success_and_above_cap_is_failure(self):
        with patch.object(checkpoints, 'MAX_GIT_OUTPUT', 128, create=True):
            rc, out = await self.run_python("import sys; sys.stdout.write('x'*128)")
            self.assertEqual((rc, out), (0, 'x' * 128))
            rc, out = await self.run_python("import sys; sys.stdout.write('x'*129)")
            self.assertEqual(rc, 125)
            self.assertIn('output', out.lower())

    async def test_nonzero_and_utf8_small_output_unchanged(self):
        rc, out = await self.run_python("import sys; sys.stdout.buffer.write('Việt'.encode()); sys.exit(7)")
        self.assertEqual((rc, out), (7, 'Việt'))

    async def test_excess_output_cannot_be_mistaken_for_missing_ref(self):
        # A failed show-ref with rc=1 denotes a missing ref; truncated output
        # must instead become an error, or snapshot may create a false initial commit.
        with patch.object(checkpoints, 'MAX_GIT_OUTPUT', 128, create=True):
            rc, out = await self.run_python("import sys; sys.stdout.write('x'*129); sys.exit(1)")
        self.assertEqual(rc, 125)
        self.assertIn('output', out.lower())

    async def test_stderr_counts_toward_same_cap(self):
        with patch.object(checkpoints, 'MAX_GIT_OUTPUT', 128, create=True):
            rc, out = await self.run_python("import sys; sys.stdout.write('a'*64); sys.stdout.flush(); sys.stderr.write('b'*65)")
        self.assertEqual(rc, 125)
        self.assertIn('output', out.lower())

    async def test_read_error_attempts_direct_child_cleanup(self):
        proc = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=OSError('fixture read failure'))
        proc.wait = AsyncMock(return_value=-9)
        proc.returncode = None
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=proc), \
             self.assertLogs('engine.checkpoints', level='ERROR'):
            with self.assertRaisesRegex(OSError, 'fixture read failure'):
                await checkpoints._run([])
        proc.kill.assert_called_once()

    async def test_timeout_after_large_output_still_cleans_child(self):
        processes = []
        original = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            return proc
        code = "import sys,time; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush(); time.sleep(30)"
        try:
            with patch.object(checkpoints, 'GIT', sys.executable), \
                 patch.object(checkpoints, 'GIT_COMMAND_TIMEOUT', .5), \
                 patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=spawn):
                result = await asyncio.wait_for(checkpoints._run(['-c', code]), 5)
            self.assertEqual(result, (124, 'timeout'))
            self.assertIsNotNone(processes[0].returncode)
        finally:
            for proc in processes:
                if proc.returncode is None:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 3)

    async def test_snapshot_fails_if_git_output_exceeds_cap(self):
        with tempfile.TemporaryDirectory(prefix='forge-git-output-') as td:
            root = Path(td)
            workspace = root / 'ws'
            workspace.mkdir()
            original = asyncio.create_subprocess_exec
            async def noisy(*args, **kwargs):
                return await original(sys.executable, '-c', "print('x'*1000)", **kwargs)
            outcome = {}
            with patch.object(checkpoints, 'GIT', sys.executable), \
                 patch.object(checkpoints, 'MAX_GIT_OUTPUT', 128, create=True), \
                 patch.object(checkpoints, '_repo_dir', return_value=root / 'repo.git'), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
                 patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=noisy):
                result = await checkpoints.snapshot('fixture', workspace, 'fixture', outcome=outcome)
            self.assertIsNone(result)
            self.assertEqual(outcome['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
