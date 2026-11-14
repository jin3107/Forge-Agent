"""Undo preflight accepts moderately large trees while remaining fail-closed."""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class UndoLargeTree(unittest.IsolatedAsyncioTestCase):
    async def test_tree_limit_is_bounded_and_default_limit_unchanged(self):
        env = dict(os.environ)
        with patch.object(checkpoints, 'GIT', sys.executable):
            rc, _ = await checkpoints._run_with_env(['-c', "print('x'*70000)"], env)
            self.assertEqual(rc, 125)
            code = f"import sys; sys.stdout.write('x'*{checkpoints.MAX_GIT_TREE_OUTPUT + 1})"
            rc, out = await asyncio.wait_for(checkpoints._run_with_env(['-c', code], env,
                max_output=checkpoints.MAX_GIT_TREE_OUTPUT), 10)
        self.assertEqual(rc, 125)
        self.assertIn('output', out.lower())
        self.assertLess(len(out), 1000)

    async def test_large_safe_tree_and_large_legacy_exclusion(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        for excluded in (False, True):
            with self.subTest(excluded=excluded), tempfile.TemporaryDirectory(prefix='forge-undo-large-') as td:
                root = Path(td)
                ws = root / 'ws'
                ws.mkdir()
                repo = root / 'checkpoint.git'
                tracked = ws / 'tracked.txt'
                for i in range(1200):
                    (ws / f'file-{i:04d}-long-ordinary-filename.txt').write_text('fixture')
                with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                     patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                    tracked.write_text('first')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                    base = [checkpoints.GIT, '--git-dir', str(repo), '--work-tree', str(ws)]
                    def git(*args):
                        return subprocess.run(base + list(args), text=True, capture_output=True, check=True).stdout.strip()
                    if excluded:
                        target = ws / 'zzz' / '.venv' / 'important.txt'
                        target.parent.mkdir(parents=True)
                        target.write_text('legacy')
                        git('add', '-f', '--', 'zzz/.venv/important.txt')
                        tree = git('write-tree')
                        parent = git('rev-parse', 'refs/heads/forge')
                        commit = git('commit-tree', tree, '-p', parent, '-m', 'legacy')
                        git('update-ref', 'refs/heads/forge', commit)
                    tracked.write_text('second')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                    parent = git('rev-parse', 'refs/heads/forge^')
                    listing = subprocess.run(base + ['ls-tree', '-r', '-z', parent], capture_output=True, check=True).stdout
                    self.assertGreater(len(listing), 64_000)
                    tracked.write_text('working')
                    if excluded:
                        target.write_text('user data')
                    result = await checkpoints.undo('fixture', ws)
                if excluded:
                    self.assertFalse(result['ok'], result)
                    self.assertIn('loại trừ', result['message'])
                    self.assertEqual(target.read_text(), 'user data')
                    self.assertEqual(tracked.read_text(), 'working')
                else:
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(tracked.read_text(), 'second')


if __name__ == '__main__':
    unittest.main()
