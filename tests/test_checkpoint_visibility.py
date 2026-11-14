"""Checkpoint outcome is visible without changing tool execution success semantics."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from engine import checkpoints, tools


class CheckpointVisibility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-checkpoint-visibility-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.repo = self.root / 'checkpoint.git'

    async def test_status_distinguishes_disabled_unavailable_failure_success(self):
        outcomes = []
        with patch.object(checkpoints, '_repo_dir', return_value=self.repo):
            for git, cfg in ((None, {'checkpoints_enabled': True}),
                             ('git', {'checkpoints_enabled': False})):
                outcome = {}
                with patch.object(checkpoints, 'GIT', git), patch('engine.checkpoints.config.load_config', return_value=cfg):
                    result = await checkpoints.snapshot('s', self.ws, 'fixture', outcome=outcome)
                self.assertIsNone(result)
                outcomes.append(outcome['status'])
            outcome = {}
            with patch.object(checkpoints, 'GIT', 'git'), \
                 patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
                 patch.object(checkpoints, '_run_with_env', new_callable=AsyncMock, return_value=(124, 'timeout')):
                result = await checkpoints.snapshot('s', self.ws, 'fixture', outcome=outcome)
            self.assertIsNone(result)
            outcomes.append(outcome['status'])
            if checkpoints.available():
                outcome = {}
                with patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}):
                    result = await checkpoints.snapshot('s', self.ws, 'fixture', outcome=outcome)
                self.assertIsNotNone(result)
                outcomes.append(outcome['status'])
        self.assertEqual(outcomes[:3], ['unavailable', 'disabled', 'failed'])
        if len(outcomes) == 4:
            self.assertEqual(outcomes[3], 'created')

    async def test_mutating_tools_show_warning_but_preserve_result(self):
        async def failed(*args, outcome=None, **kwargs):
            outcome['status'] = 'failed'
            return None
        cases = [('write_file', {'path': 'a.txt', 'content': 'hello'}),
                 ('edit_file', {'path': 'a.txt', 'old_text': 'hello', 'new_text': 'changed'}),
                 ('memory_save', {'content': 'fixture'}),
                 ('run_background', {'command': 'fixture'}),
                 ('run_command', {'command': 'fixture'})]
        async def memory(ws, args):
            return 'fixture memory saved'
        async def background(*args, **kwargs):
            return 'fixture background started'
        async def command(*args, **kwargs):
            return {'ok': True, 'output': 'exit=0', 'exit_code': 0, 'timed_out': False,
                    'blocked': False, 'truncated': False}
        with patch('engine.checkpoints.snapshot', side_effect=failed), \
             patch.dict(tools._IMPL, {'memory_save': lambda ws, args: 'fixture memory saved'}), \
             patch('engine.tools._run_background', side_effect=background), \
             patch('engine.tools._run_command_reserved', side_effect=command):
            for name, args in cases:
                with self.subTest(name=name):
                    result = await tools.execute_tool(name, args, self.ws)
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['checkpoint_status'], 'failed')
                    self.assertIn('checkpoint', result['output'].lower())
                    self.assertIn('undo', result['output'].lower())
        self.assertEqual((self.ws / 'a.txt').read_text(), 'changed')

    async def test_real_snapshot_failure_warns_and_file_write_still_occurs(self):
        with patch.object(checkpoints, 'GIT', 'git'), \
             patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.checkpoints.config.load_config', return_value={'checkpoints_enabled': True}), \
             patch.object(checkpoints, '_run_with_env', new_callable=AsyncMock, return_value=(124, 'timeout')):
            result = await tools.execute_tool('write_file', {'path': 'actual.txt', 'content': 'saved'}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['checkpoint_status'], 'failed')
        self.assertIn('Undo may not restore', result['output'])
        self.assertEqual((self.ws / 'actual.txt').read_text(), 'saved')

    async def test_enabled_disabled_and_unavailable_warning_matches_status(self):
        for status in ('disabled', 'unavailable', 'failed', 'created'):
            async def snapshot(*args, outcome=None, **kwargs):
                outcome['status'] = status
                return '1234567890' if status == 'created' else None
            with self.subTest(status=status), patch('engine.checkpoints.snapshot', side_effect=snapshot):
                result = await tools.execute_tool('write_file', {'path': f'{status}.txt', 'content': 'x'}, self.ws)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['checkpoint_status'], status)
            if status == 'created':
                self.assertNotIn('undo', result['output'].lower())
            else:
                self.assertIn('undo', result['output'].lower())

    async def test_full_foreground_quota_does_not_warn_checkpoint_failure(self):
        with patch.object(tools, 'MAX_FOREGROUND_RUNNING', 1), \
             patch.object(tools, '_FOREGROUND_INFLIGHT', 1), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            result = await tools.execute_tool('run_command', {'command': 'echo fixture'}, self.ws)
        self.assertTrue(result['blocked'])
        self.assertNotIn('checkpoint_status', result)
        self.assertNotIn('undo', result['output'].lower())
        snap.assert_not_awaited()

    async def test_terminal_returns_status_and_warning(self):
        from server.main import ExecBody, workspace_exec
        async def snapshot(*args, outcome=None, **kwargs):
            outcome['status'] = 'failed'
        async def command(*args, **kwargs):
            return {'ok': True, 'output': 'exit=0', 'exit_code': 0, 'timed_out': False,
                    'blocked': False, 'truncated': False}
        with patch('server.main._ws', return_value=self.ws), \
             patch('engine.checkpoints.snapshot', side_effect=snapshot), \
             patch('engine.tools._run_command_reserved', side_effect=command), \
             patch('server.main.guard.assess_command', return_value={'level': 'safe', 'reasons': []}):
            result = await workspace_exec('s', ExecBody(command='echo fixture'), user=None)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['checkpoint_status'], 'failed')
        self.assertIn('undo', result['output'].lower())

    async def test_nonmutating_tool_has_no_checkpoint_status(self):
        result = await tools.execute_tool('list_dir', {'path': '.'}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('checkpoint_status', result)


if __name__ == '__main__':
    unittest.main()
