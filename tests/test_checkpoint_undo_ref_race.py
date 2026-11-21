"""Undo must not overwrite a checkpoint ref changed by another writer."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class CheckpointUndoRefRace(unittest.IsolatedAsyncioTestCase):
    async def test_undo_supplies_the_original_ref_as_expected_old_value(self):
        calls = []
        async def fake(args, env, **kwargs):
            calls.append(args[4:])
            if args[4] == 'rev-parse':
                return 0, 'a' * 40
            if args[4] == 'rev-list':
                return 0, 'a' * 40 + ' ' + 'b' * 40
            return 0, ''
        with patch.object(checkpoints, 'GIT', 'git'), patch.object(checkpoints, '_run_with_env', side_effect=fake):
            result = await checkpoints.undo('fixture', Path('/fixture'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(calls[-1], ['update-ref', 'refs/heads/forge', 'b' * 40, 'a' * 40])

    async def test_missing_or_invalid_git_object_ids_abort_before_mutation(self):
        valid = 'a' * 40
        cases = [('current', ''), ('current', 'f' * 39), ('current', 'warning'),
                 ('ancestry', ''), ('ancestry', 'b' * 40),
                 ('ancestry', valid + ' ' + 'f' * 64),
                 ('ancestry', valid + ' ' + 'b' * 40 + ' ' + 'c' * 40)]
        for field, invalid in cases:
            calls = []
            async def fake(args, env, **kwargs):
                calls.append(args[4:])
                if args[4] == 'rev-parse':
                    return 0, invalid if field == 'current' else valid
                if args[4] == 'rev-list':
                    return 0, invalid
                return 0, ''
            with self.subTest(field=field, value=invalid), \
                 patch.object(checkpoints, 'GIT', 'git'), \
                 patch.object(checkpoints, '_run_with_env', side_effect=fake):
                result = await checkpoints.undo('fixture', Path('/fixture'))
            self.assertFalse(result['ok'], result)
            self.assertEqual([c[0] for c in calls], ['rev-parse'] if field == 'current' else ['rev-parse', 'rev-list'])

    async def test_root_pop_does_not_delete_concurrently_changed_ref(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with tempfile.TemporaryDirectory(prefix='forge-root-race-') as td:
            root = Path(td)
            ws = root / 'ws'
            ws.mkdir()
            repo = root / 'ckpt.git'
            path = ws / 'tracked.txt'
            base = [checkpoints.GIT, '--git-dir', str(repo), '--work-tree', str(ws)]
            def git(*args):
                return subprocess.run(base + list(args), text=True, capture_output=True, check=True).stdout.strip()
            with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                path.write_text('before')
                self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'root'))
                initial = git('rev-parse', 'refs/heads/forge')
                tree = git('rev-parse', initial + '^{tree}')
                concurrent = git('commit-tree', tree, '-p', initial, '-m', 'concurrent')
                path.write_text('after')
                original = checkpoints._run_with_env
                changed = False
                async def injected(args, env, **kwargs):
                    nonlocal changed
                    if args[4:6] == ['update-ref', '-d']:
                        changed = True
                        rc, output = await original(args[:4] + ['update-ref', 'refs/heads/forge', concurrent], env)
                        self.assertEqual(rc, 0, output)
                    return await original(args, env, **kwargs)
                with patch.object(checkpoints, '_run_with_env', side_effect=injected):
                    result = await checkpoints.undo('fixture', ws)
                final_ref = git('rev-parse', 'refs/heads/forge')
            self.assertTrue(changed)
            self.assertFalse(result['ok'], result)
            self.assertIn('update-ref', result['message'])
            self.assertEqual(final_ref, concurrent)
            self.assertEqual(path.read_text(), 'before')

    async def test_real_git_ref_change_or_deletion_is_not_overwritten(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        for mode in ('changed', 'deleted'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='forge-undo-race-') as td:
                root = Path(td)
                ws = root / 'ws'
                ws.mkdir()
                repo = root / 'ckpt.git'
                path = ws / 'tracked.txt'
                base = [checkpoints.GIT, '--git-dir', str(repo), '--work-tree', str(ws)]
                def git(*args, check=True):
                    return subprocess.run(base + list(args), text=True, capture_output=True, check=check)
                with patch.object(checkpoints, '_repo_dir', return_value=repo), \
                     patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                    path.write_text('first')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'first'))
                    first = git('rev-parse', 'refs/heads/forge').stdout.strip()
                    path.write_text('second')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'second'))
                    path.write_text('third')
                    self.assertIsNotNone(await checkpoints.snapshot('fixture', ws, 'third'))
                    path.write_text('working')
                    original = checkpoints._run_with_env
                    changed = False
                    async def injected(args, env, **kwargs):
                        nonlocal changed
                        if args[4] == 'update-ref' and not changed:
                            changed = True
                            command = ['update-ref', 'refs/heads/forge', first] if mode == 'changed' else ['update-ref', '-d', 'refs/heads/forge']
                            rc, out = await original(args[:4] + command, env)
                            self.assertEqual(rc, 0, out)
                        return await original(args, env, **kwargs)
                    with patch.object(checkpoints, '_run_with_env', side_effect=injected):
                        result = await checkpoints.undo('fixture', ws)
                    after = git('rev-parse', '--verify', 'refs/heads/forge', check=False)
                self.assertTrue(changed)
                self.assertFalse(result['ok'], result)
                self.assertIn('update-ref', result['message'])
                self.assertIn('có thể', result['message'])
                # The preceding checkout has already changed the workspace;
                # only the concurrent writer's ref value is protected.
                self.assertEqual(path.read_text(), 'third')
                if mode == 'changed':
                    self.assertEqual(after.returncode, 0)
                    self.assertEqual(after.stdout.strip(), first)
                else:
                    self.assertNotEqual(after.returncode, 0)


if __name__ == '__main__':
    unittest.main()
