"""Preflight undo to avoid overwriting excluded paths tracked in old snapshots."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints
from _win_compat import newline_in_filenames_supported


class UndoTrackedExcludes(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_tracked_exclusion_blocks_before_mutation(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        relatives = ['.venv/important.txt']
        if newline_in_filenames_supported():  # Windows cannot create such names
            relatives.append('folder\nname/node_modules/important.txt')
        for relative in relatives:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(prefix='forge-undo-legacy-') as td:
                root = Path(td)
                ws = root / 'workspace'
                ws.mkdir()
                repo = root / 'checkpoint.git'
                ordinary = ws / 'ordinary.txt'
                protected = ws / relative
                with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                     patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                    ordinary.write_text('initial')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'initial'))
                    protected.parent.mkdir(parents=True)
                    protected.write_text('legacy')
                    base = [checkpoints.GIT, '--git-dir', str(repo), '--work-tree', str(ws)]
                    def git(*args):
                        return subprocess.run(base + list(args), check=True, text=True, capture_output=True).stdout.strip()
                    # Simulate an older snapshot with a forcibly tracked excluded file.
                    git('add', '-f', '--', relative)
                    tree = git('write-tree')
                    parent = git('rev-parse', 'refs/heads/forge')
                    legacy = git('commit-tree', tree, '-p', parent, '-m', 'legacy')
                    git('update-ref', 'refs/heads/forge', legacy)
                    ordinary.write_text('newer')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'newer'))
                    current_ref = git('rev-parse', 'refs/heads/forge')
                    protected.write_text('user current')
                    ordinary.write_text('user current ordinary')
                    untracked = ws / 'new-untracked.txt'
                    untracked.write_text('do not remove')
                    result = await checkpoints.undo('fixture', ws)
                    after_ref = git('rev-parse', 'refs/heads/forge')
                self.assertFalse(result['ok'], result)
                self.assertIn('checkpoint', result['message'].lower())
                self.assertEqual(protected.read_text(), 'user current')
                self.assertEqual(ordinary.read_text(), 'user current ordinary')
                self.assertEqual(untracked.read_text(), 'do not remove')
                self.assertEqual(after_ref, current_ref)

    async def test_real_preflight_over_output_cap_aborts_without_mutation(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory(prefix='forge-undo-tree-cap-') as td:
            root = Path(td)
            ws = root / 'ws'
            ws.mkdir()
            file = ws / 'tracked.txt'
            for i in range(6):
                (ws / f'file-{i}.txt').write_text('entry')
            with patch.object(checkpoints, '_repo_dir', return_value=root / 'checkpoint.git'), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                file.write_text('first')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                file.write_text('second')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                file.write_text('user change')
                with patch.object(checkpoints, 'MAX_GIT_TREE_OUTPUT', 128):
                    result = await checkpoints.undo('fixture', ws)
            self.assertFalse(result['ok'], result)
            self.assertEqual(file.read_text(), 'user change')

    async def test_preflight_errors_and_incomplete_output_abort_without_mutation(self):
        cases = [(124, 'timeout'), (125, 'checkpoint Git output exceeded limit'),
                 (1, 'git failure'), (0, 'missing-null-terminator'),
                 (0, 'git warning\n100644 blob ' + 'a' * 40 + '\t.venv/file\0'),
                 (0, '100644 blob ' + 'a' * 40 + '\tordinary\0\0')]
        for status, output in cases:
            calls = []
            async def fake(args, env, **kwargs):
                action = args[4]
                calls.append(action)
                if action == 'rev-parse':
                    return 0, 'a' * 40
                if action == 'rev-list':
                    return 0, 'a' * 40 + ' ' + 'b' * 40
                if action == 'ls-tree':
                    return status, output
                return 0, ''
            with self.subTest(status=status, output=output), \
                 patch.object(checkpoints, 'GIT', 'git'), \
                 patch.object(checkpoints, '_run_with_env', side_effect=fake):
                result = await checkpoints.undo('fixture', Path('/fixture'))
            self.assertFalse(result['ok'], result)
            self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree'])

    async def test_preflight_exception_aborts_without_mutation(self):
        calls = []
        async def fake(args, env, **kwargs):
            action = args[4]
            calls.append(action)
            if action == 'rev-parse':
                return 0, 'a' * 40
            if action == 'rev-list':
                return 0, 'a' * 40 + ' ' + 'b' * 40
            if action == 'ls-tree':
                raise OSError('private data')
            return 0, ''
        with patch.object(checkpoints, 'GIT', 'git'), patch.object(checkpoints, '_run_with_env', side_effect=fake):
            result = await checkpoints.undo('fixture', Path('/fixture'))
        self.assertFalse(result['ok'])
        self.assertNotIn('private data', result['message'])
        self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree'])

    async def test_case_insensitive_match_is_conservative(self):
        calls = []
        async def fake(args, env, **kwargs):
            action = args[4]
            calls.append(action)
            if action == 'rev-parse':
                return 0, 'a' * 40
            if action == 'rev-list':
                return 0, 'a' * 40 + ' ' + 'b' * 40
            if action == 'ls-tree':
                return 0, '100644 blob ' + 'a' * 40 + '\tdirectory/.VENV/file\0'
            return 0, ''
        with patch.object(checkpoints, 'GIT', 'git'), patch.object(checkpoints, '_run_with_env', side_effect=fake):
            result = await checkpoints.undo('fixture', Path('/fixture'))
        self.assertFalse(result['ok'])
        self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree'])


if __name__ == '__main__':
    unittest.main()
