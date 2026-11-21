"""Direct workspace saves checkpoint before writing and disclose failures."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from engine import checkpoints
from server.main import FileSaveBody, save_workspace_file


class WorkspaceSaveCheckpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-save-checkpoint-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.repo = self.root / 'checkpoint.git'
        self.target = self.ws / 'file.txt'
        self.target.write_text('before')

    async def test_real_git_snapshot_then_save_can_be_undone(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        with patch('server.main._ws', return_value=self.ws), \
             patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            result = await save_workspace_file('fixture', FileSaveBody(path='file.txt', content='after'), user=None)
            self.assertTrue(result['ok'])
            self.assertEqual(result['checkpoint_status'], 'created')
            self.assertIn('ignored', result['notice'].lower())
            self.assertEqual(self.target.read_text(), 'after')
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertEqual(self.target.read_text(), 'before')

    async def test_created_checkpoint_does_not_promise_ignored_file_undo(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        (self.ws / '.gitignore').write_text('ignored.txt\n')
        ignored = self.ws / 'ignored.txt'
        ignored.write_text('before')
        with patch('server.main._ws', return_value=self.ws), \
             patch.object(checkpoints, '_repo_dir', return_value=self.repo), \
             patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            result = await save_workspace_file('fixture', FileSaveBody(path='ignored.txt', content='after'), user=None)
            self.assertEqual(result['checkpoint_status'], 'created')
            self.assertIn('ignored', result['notice'].lower())
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertEqual(ignored.read_text(), 'after')

    async def test_unconfirmed_snapshot_warns_but_saves(self):
        for status in ('failed', 'disabled', 'unavailable'):
            async def snapshot(*args, outcome=None, **kwargs):
                outcome['status'] = status
                return None
            with self.subTest(status=status), patch('server.main._ws', return_value=self.ws), \
                 patch('engine.checkpoints.snapshot', side_effect=snapshot):
                result = await save_workspace_file('fixture', FileSaveBody(path='file.txt', content=status), user=None)
            self.assertTrue(result['ok'])
            self.assertEqual(result['checkpoint_status'], status)
            self.assertIn('undo', result['warning'].lower())
            self.assertEqual(self.target.read_text(), status)

    async def test_snapshot_runs_before_write_and_exactly_once(self):
        async def snapshot(*args, outcome=None, **kwargs):
            self.assertEqual(self.target.read_text(), 'before')
            self.assertEqual(args[1], self.ws)
            outcome['status'] = 'created'
            return 'abc123'
        with patch('server.main._ws', return_value=self.ws), \
             patch('engine.checkpoints.snapshot', side_effect=snapshot) as snap:
            result = await save_workspace_file('fixture', FileSaveBody(path='file.txt', content='after'), user=None)
        self.assertEqual(snap.await_count, 1)
        self.assertEqual(result['checkpoint_status'], 'created')
        self.assertNotIn('warning', result)
        self.assertEqual(self.target.read_text(), 'after')

    async def test_invalid_path_rejected_before_checkpoint(self):
        with patch('server.main._ws', return_value=self.ws), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            with self.assertRaises(HTTPException) as err:
                await save_workspace_file('fixture', FileSaveBody(path='../outside', content='bad'), user=None)
        self.assertEqual(err.exception.status_code, 400)
        snap.assert_not_awaited()
        self.assertFalse((self.root / 'outside').exists())

    async def test_snapshot_exception_does_not_write(self):
        with patch('server.main._ws', return_value=self.ws), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock, side_effect=OSError('private path')):
            with self.assertRaises(HTTPException) as err:
                await save_workspace_file('fixture', FileSaveBody(path='file.txt', content='after'), user=None)
        self.assertNotIn('private path', str(err.exception.detail))
        self.assertEqual(self.target.read_text(), 'before')

    async def test_cancelled_snapshot_does_not_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def snapshot(*args, **kwargs):
            entered.set()
            await release.wait()
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            task = asyncio.create_task(save_workspace_file('fixture', FileSaveBody(path='file.txt', content='after'), user=None))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.target.read_text(), 'before')

    @unittest.skipUnless(os.name == 'posix', 'fixture uses symlinks')
    async def test_symlink_escape_introduced_during_checkpoint_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'file.txt').write_text('outside')
        async def snapshot(*args, outcome=None, **kwargs):
            self.target.unlink()
            self.target.symlink_to(outside / 'file.txt')
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            with self.assertRaises(HTTPException):
                await save_workspace_file('fixture', FileSaveBody(path='file.txt', content='bad'), user=None)
        self.assertEqual((outside / 'file.txt').read_text(), 'outside')

    async def test_workspace_changed_during_checkpoint_is_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        with patch('server.main._ws', side_effect=[self.ws, other]), \
             patch('engine.checkpoints.snapshot', new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as err:
                await save_workspace_file('fixture', FileSaveBody(path='file.txt', content='after'), user=None)
        self.assertEqual(err.exception.status_code, 409)
        self.assertEqual(self.target.read_text(), 'before')


if __name__ == '__main__':
    unittest.main()
