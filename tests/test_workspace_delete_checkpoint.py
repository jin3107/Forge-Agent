"""Direct workspace delete checkpoints, warning and safety regressions."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from engine import checkpoints
from server.main import delete_workspace_file

class WorkspaceDeleteCheckpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-delete-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.repo = self.root / 'checkpoint.git'
        self.target = self.ws / 'file.txt'
        self.target.write_text('original')

    async def test_real_git_delete_then_undo(self):
        if not checkpoints.available(): self.skipTest('git unavailable')
        with patch('server.main._ws', return_value=self.ws), patch.object(checkpoints, '_repo_dir', return_value=self.repo), patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            r = await delete_workspace_file('sid', 'file.txt', user=None)
            self.assertEqual(r['checkpoint_status'], 'created')
            self.assertIn('ignored', r['notice'].lower())
            self.assertFalse(self.target.exists())
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertEqual(self.target.read_text(), 'original')

    async def test_ignored_file_not_restored_despite_created(self):
        if not checkpoints.available(): self.skipTest('git unavailable')
        (self.ws / '.gitignore').write_text('file.txt\n')
        with patch('server.main._ws', return_value=self.ws), patch.object(checkpoints, '_repo_dir', return_value=self.repo), patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            r = await delete_workspace_file('sid', 'file.txt', user=None)
            self.assertEqual(r['checkpoint_status'], 'created')
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertFalse(self.target.exists())

    async def test_order_and_statuses(self):
        for status in ('created', 'disabled', 'unavailable', 'failed'):
            self.target.write_text('original')
            async def snapshot(*args, outcome=None, **kwargs):
                self.assertTrue(self.target.exists())
                outcome['status'] = status
            with self.subTest(status=status), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot) as snap:
                r = await delete_workspace_file('sid', 'file.txt', user=None)
                self.assertEqual(snap.await_count, 1)
                self.assertEqual(r['checkpoint_status'], status)
                self.assertIn('notice' if status == 'created' else 'warning', r)
                self.assertFalse(self.target.exists())

    async def test_invalid_missing_directory_before_checkpoint(self):
        for path, status in [('../outside', 400), ('missing', 404), ('.', 400)]:
            with self.subTest(path=path), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
                with self.assertRaises(HTTPException) as e:
                    await delete_workspace_file('sid', path, user=None)
                self.assertEqual(e.exception.status_code, status)
                snap.assert_not_awaited()

    async def test_exception_and_cancellation_do_not_delete(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock, side_effect=OSError('secret')):
            with self.assertRaises(HTTPException) as e: await delete_workspace_file('sid', 'file.txt', user=None)
            self.assertNotIn('secret', str(e.exception.detail))
        entered = asyncio.Event()
        async def snapshot(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            task = asyncio.create_task(delete_workspace_file('sid', 'file.txt', user=None))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(self.target.exists())

    async def test_workspace_changed(self):
        other = self.root / 'other'
        other.mkdir()
        with patch('server.main._ws', side_effect=[self.ws, other]), patch('engine.checkpoints.snapshot', new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as e: await delete_workspace_file('sid', 'file.txt', user=None)
            self.assertEqual(e.exception.status_code, 409)
        self.assertTrue(self.target.exists())

    @unittest.skipUnless(os.name == 'posix', 'requires symlinks')
    async def test_symlink_escape_during_checkpoint(self):
        outside = self.root / 'outside'
        outside.write_text('outside')
        async def snapshot(*args, **kwargs):
            self.target.unlink()
            self.target.symlink_to(outside)
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            with self.assertRaises(HTTPException): await delete_workspace_file('sid', 'file.txt', user=None)
        self.assertEqual(outside.read_text(), 'outside')

    async def test_disappearance_during_checkpoint(self):
        async def snapshot(*args, **kwargs): self.target.unlink()
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            with self.assertRaises(HTTPException) as e: await delete_workspace_file('sid', 'file.txt', user=None)
        self.assertEqual(e.exception.status_code, 409)
