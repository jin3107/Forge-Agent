"""Checkpoint snapshot reports success only after every required Git step succeeds."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints


class CheckpointSnapshotResults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-snapshot-results-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.repo = self.root / 'checkpoint.git'

    def fake_runner(self, failed=None, status=1, existing=False, exception=False):
        calls = []
        async def run(args, env, **kwargs):
            action = args[4]
            tag = action
            if action == 'rev-parse':
                tag = 'probe' if args[-1] == '--is-inside-work-tree' else 'parent'
            elif action == 'config':
                tag = args[5]
            calls.append((tag, args[4:]))
            if tag == failed:
                if exception:
                    raise OSError('fixture private failure')
                return status, 'fixture failure'
            if tag == 'probe':
                return (0, 'true') if existing else (128, 'missing')
            if tag == 'show-ref':
                return (0, '') if existing else (1, '')
            if tag == 'parent':
                return 0, 'a' * 40
            if tag == 'write-tree':
                return 0, 'b' * 40
            if tag == 'commit-tree':
                return 0, 'c' * 40
            return 0, ''
        return run, calls

    async def call_snapshot(self, runner):
        with patch.object(checkpoints, 'GIT', 'git'), \
             patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch.object(checkpoints, '_run_with_env', side_effect=runner):
            return await checkpoints.snapshot('fixture', self.ws, 'test')

    async def test_init_and_config_failures_abort(self):
        for failed in ('init', 'user.email', 'user.name'):
            for status in (1, 124):
                with self.subTest(failed=failed, status=status):
                    run, calls = self.fake_runner(failed, status)
                    result = await self.call_snapshot(run)
                    self.assertIsNone(result)
                    self.assertEqual(calls[-1][0], failed)
                    self.assertNotIn('add', [x[0] for x in calls])

    async def test_stage_tree_ref_commit_update_failure_abort(self):
        for failed in ('add', 'write-tree', 'show-ref', 'parent', 'commit-tree', 'update-ref'):
            for status in (1, 124):
                if failed == 'show-ref' and status == 1:
                    continue  # show-ref 1 means the branch does not exist
                with self.subTest(failed=failed, status=status):
                    run, calls = self.fake_runner(failed, status, existing=True)
                    result = await self.call_snapshot(run)
                    self.assertIsNone(result)
                    self.assertEqual(calls[-1][0], failed)
                    if failed == 'add':
                        self.assertEqual(sum(c[0] == 'add' for c in calls), 1)
                        self.assertIn(':(exclude)node_modules', calls[-1][1])

    async def test_probe_timeout_does_not_attempt_init(self):
        run, calls = self.fake_runner('probe', 124)
        self.assertIsNone(await self.call_snapshot(run))
        self.assertEqual([x[0] for x in calls], ['probe'])

    async def test_broken_existing_repo_does_not_attempt_reinit(self):
        self.repo.mkdir()
        run, calls = self.fake_runner('probe', 128)
        self.assertIsNone(await self.call_snapshot(run))
        self.assertEqual([x[0] for x in calls], ['probe'])

    async def test_spawn_exception_is_failure(self):
        run, calls = self.fake_runner('update-ref', exception=True, existing=True)
        self.assertIsNone(await self.call_snapshot(run))
        self.assertEqual(calls[-1][0], 'update-ref')

    async def test_new_and_existing_refs_compare_expected_old_value(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                run, calls = self.fake_runner(existing=existing)
                self.assertEqual(await self.call_snapshot(run), 'c' * 10)
                update = calls[-1][1]
                self.assertEqual(update, ['update-ref', 'refs/heads/forge', 'c' * 40,
                                          'a' * 40 if existing else ''])

    async def test_real_git_changed_ref_is_not_overwritten(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        original = checkpoints._run_with_env
        deleted = False
        async def concurrent_change(args, env, **kwargs):
            nonlocal deleted
            if args[4] == 'update-ref' and not deleted:
                deleted = True
                rc, out = await original(args[:4] + ['update-ref', '-d', 'refs/heads/forge'], env)
                self.assertEqual(rc, 0, out)
            return await original(args, env, **kwargs)
        with patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
            (self.ws / 'state.txt').write_text('first')
            self.assertIsNotNone(await checkpoints.snapshot('fixture', self.ws, 'first'))
            (self.ws / 'state.txt').write_text('second')
            with patch.object(checkpoints, '_run_with_env', side_effect=concurrent_change):
                second = await checkpoints.snapshot('fixture', self.ws, 'second')
            listing = await checkpoints.list_checkpoints('fixture', self.ws)
        self.assertTrue(deleted)
        self.assertIsNone(second)
        self.assertEqual(listing, [])

    async def test_real_git_snapshots_form_chain(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
            (self.ws / 'state.txt').write_text('first')
            first = await checkpoints.snapshot('fixture', self.ws, 'first')
            (self.ws / 'state.txt').write_text('second')
            second = await checkpoints.snapshot('fixture', self.ws, 'second')
            listing = await checkpoints.list_checkpoints('fixture', self.ws)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(listing), 2)
        self.assertTrue(second.startswith(listing[0].split()[0]), listing)
        self.assertTrue(first.startswith(listing[1].split()[0]), listing)


if __name__ == '__main__':
    unittest.main()
