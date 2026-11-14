"""Undo must not clean workspace paths intentionally excluded from snapshots."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class CheckpointUndoExcludes(unittest.IsolatedAsyncioTestCase):
    async def test_real_git_undo_preserves_excluded_directories(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory(prefix='forge-undo-excludes-') as td:
            root = Path(td)
            ws = root / 'workspace'
            ws.mkdir()
            repo = root / 'checkpoint.git'
            tracked = ws / 'tracked.txt'
            # These are excluded before the snapshots and should still be safe
            # if created or modified afterwards; nothing relies on .gitignore.
            preserved = []
            for name in ('node_modules', '.venv', 'venv', '__pycache__', 'dist',
                         'build', 'target', '.next', '.cache', '.ckpt-fixture'):
                path = ws / name / 'important.txt'
                path.parent.mkdir(parents=True)
                path.write_text('before')
                preserved.append(path)
            with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                tracked.write_text('first')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                tracked.write_text('second')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                for path in preserved:
                    path.write_text('after')
                (ws / 'ordinary-untracked.txt').write_text('discard')
                result = await checkpoints.undo('fixture', ws)
            self.assertTrue(result['ok'], result)
            self.assertEqual(tracked.read_text(), 'second')
            self.assertFalse((ws / 'ordinary-untracked.txt').exists())
            for path in preserved:
                with self.subTest(path=path):
                    self.assertTrue(path.exists(), f'undo removed excluded {path}')
                    self.assertEqual(path.read_text(), 'after')

    async def test_real_git_undo_preserves_nested_excluded_directories(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory(prefix='forge-undo-nested-') as td:
            root = Path(td)
            ws = root / 'workspace'
            ws.mkdir()
            repo = root / 'checkpoint.git'
            tracked = ws / 'tracked.txt'
            with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                tracked.write_text('first')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                tracked.write_text('second')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                preserved = []
                for name in ('node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build',
                             'target', '.next', '.cache', '.ckpt-nested'):
                    nested = ws / 'app' / name / 'important.txt'
                    nested.parent.mkdir(parents=True)
                    nested.write_text('keep')
                    preserved.append(nested)
                other = ws / 'app' / 'scratch.txt'
                other.write_text('remove')
                result = await checkpoints.undo('fixture', ws)
            self.assertTrue(result['ok'], result)
            for nested in preserved:
                with self.subTest(path=nested):
                    self.assertTrue(nested.exists(), f'undo removed {nested}')
                    self.assertEqual(nested.read_text(), 'keep')
            self.assertFalse(other.exists())

    @unittest.skipUnless(os.name == 'posix', 'fixture uses POSIX symlinks')
    async def test_excluded_untracked_symlink_is_not_deleted(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory(prefix='forge-undo-link-') as td:
            root = Path(td)
            ws = root / 'workspace'
            ws.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'important.txt').write_text('keep')
            repo = root / 'checkpoint.git'
            tracked = ws / 'tracked.txt'
            with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                tracked.write_text('first')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                tracked.write_text('second')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                link = ws / '.venv'
                link.symlink_to(outside, target_is_directory=True)
                result = await checkpoints.undo('fixture', ws)
            self.assertTrue(result['ok'], result)
            self.assertTrue(link.is_symlink())
            self.assertEqual((outside / 'important.txt').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
