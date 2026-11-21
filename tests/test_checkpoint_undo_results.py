"""Undo must report and stop on failures of mutating Git steps."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class CheckpointUndoResults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-checkpoint-undo-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()

    async def test_each_failed_mutation_stops_and_reports_partial_change(self):
        steps = ['read-tree', 'checkout-index', 'clean', 'update-ref']
        for failed in steps:
            for status in (1, 124):
                with self.subTest(step=failed, status=status):
                    calls = []
                    async def fake(args, env, **kwargs):
                        action = args[4]
                        calls.append(action)
                        if action == 'rev-parse':
                            return 0, 'a' * 40
                        if action == 'rev-list':
                            return 0, 'a' * 40 + ' ' + 'b' * 40
                        return (status, 'failure details') if action == failed else (0, '')
                    with patch.object(checkpoints, 'GIT', 'git'), \
                         patch.object(checkpoints, '_run_with_env', side_effect=fake):
                        result = await checkpoints.undo('fixture', self.ws)
                    self.assertFalse(result['ok'], result)
                    self.assertIn(failed, result['message'])
                    self.assertIn('có thể', result['message'])
                    self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree'] + steps[:steps.index(failed) + 1])

    async def test_spawn_exception_on_mutation_is_reported_and_stops(self):
        calls = []
        async def fake(args, env, **kwargs):
            action = args[4]
            calls.append(action)
            if action == 'rev-parse':
                return 0, 'a' * 40
            if action == 'rev-list':
                return 0, 'a' * 40 + ' ' + 'b' * 40
            if action == 'checkout-index':
                raise OSError('sensitive fixture error')
            return 0, ''
        with patch.object(checkpoints, 'GIT', 'git'), patch.object(checkpoints, '_run_with_env', side_effect=fake):
            result = await checkpoints.undo('fixture', self.ws)
        self.assertFalse(result['ok'], result)
        self.assertIn('checkout-index', result['message'])
        self.assertIn('có thể', result['message'])
        self.assertNotIn('sensitive fixture error', result['message'])
        self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree', 'read-tree', 'checkout-index'])

    async def test_success_executes_all_steps_and_reports_success(self):
        calls = []
        async def fake(args, env, **kwargs):
            action = args[4]
            calls.append(action)
            if action == 'rev-parse':
                return 0, 'a' * 40
            if action == 'rev-list':
                return 0, 'a' * 40 + ' ' + 'b' * 40
            return 0, ''
        with patch.object(checkpoints, 'GIT', 'git'), patch.object(checkpoints, '_run_with_env', side_effect=fake):
            result = await checkpoints.undo('fixture', self.ws)
        self.assertTrue(result['ok'], result)
        self.assertIn('a' * 10, result['message'])
        self.assertEqual(calls, ['rev-parse', 'rev-list', 'ls-tree', 'read-tree', 'checkout-index', 'clean', 'update-ref'])

    async def test_real_git_with_injected_ref_failure_reports_partial_undo(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        repo = self.root / 'checkpoint.git'
        path = self.ws / 'state.txt'
        original = checkpoints._run_with_env
        async def fail_update(args, env, **kwargs):
            if args[4] == 'update-ref':
                return 1, 'injected update-ref failure'
            return await original(args, env, **kwargs)
        with patch.object(checkpoints, '_repo_dir', return_value=repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
            path.write_text('first')
            self.assertIsNotNone(await checkpoints.snapshot('fixture', self.ws, 'first'))
            path.write_text('second')
            second = await checkpoints.snapshot('fixture', self.ws, 'second')
            path.write_text('changed')
            with patch.object(checkpoints, '_run_with_env', side_effect=fail_update):
                result = await checkpoints.undo('fixture', self.ws)
            listing = await checkpoints.list_checkpoints('fixture', self.ws)
        self.assertFalse(result['ok'], result)
        self.assertIn('update-ref', result['message'])
        self.assertEqual(path.read_text(), 'second')
        self.assertTrue(listing and second.startswith(listing[0].split()[0]), listing)

    async def test_real_git_undo_restores_latest_and_removes_new_file(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        repo = self.root / 'checkpoint.git'
        path = self.ws / 'state.txt'
        with patch.object(checkpoints, '_repo_dir', return_value=repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
            path.write_text('first')
            first = await checkpoints.snapshot('fixture', self.ws, 'first')
            path.write_text('second')
            second = await checkpoints.snapshot('fixture', self.ws, 'second')
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            path.write_text('changed')
            (self.ws / 'untracked.txt').write_text('new')
            result = await checkpoints.undo('fixture', self.ws)
            listing = await checkpoints.list_checkpoints('fixture', self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(path.read_text(), 'second')
        self.assertFalse((self.ws / 'untracked.txt').exists())
        self.assertTrue(listing and first.startswith(listing[0].split()[0]), listing)


if __name__ == '__main__':
    unittest.main()
