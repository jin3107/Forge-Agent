"""Direct multipart uploads take one checkpoint per mutating request."""
import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException, UploadFile
from engine import checkpoints
from server.main import upload_files


def file(name, content=b'data'):
    return UploadFile(filename=name, file=io.BytesIO(content))

class WorkspaceUploadCheckpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-upload-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.repo = self.root / 'checkpoints.git'
        (self.ws / 'old.txt').write_bytes(b'before')

    async def test_multi_upload_one_checkpoint_undo(self):
        if not checkpoints.available(): self.skipTest('git unavailable')
        with patch('server.main._ws', return_value=self.ws), patch.object(checkpoints, '_repo_dir', return_value=self.repo), patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            r = await upload_files('sid', [file('old.txt', b'after'), file('new.txt', b'new'), file('.hidden')], user=None, overwrite=True)
            self.assertEqual(r['checkpoint_status'], 'created')
            self.assertIn('ignored', r['notice'].lower())
            self.assertEqual(r['saved'], ['old.txt', 'new.txt'])
            self.assertEqual(r['skipped'], ['.hidden'])
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertEqual((self.ws / 'old.txt').read_bytes(), b'before')
        self.assertFalse((self.ws / 'new.txt').exists())

    async def test_ignored_file_created_not_undoable(self):
        if not checkpoints.available(): self.skipTest('git unavailable')
        (self.ws / '.gitignore').write_text('old.txt\n')
        with patch('server.main._ws', return_value=self.ws), patch.object(checkpoints, '_repo_dir', return_value=self.repo), patch('engine.config.load_config', return_value={'checkpoints_enabled': True}):
            r = await upload_files('sid', [file('old.txt', b'after')], user=None, overwrite=True)
            self.assertEqual(r['checkpoint_status'], 'created')
            undo = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(undo['ok'], undo)
        self.assertEqual((self.ws / 'old.txt').read_bytes(), b'after')

    async def test_status_and_order_one_snapshot(self):
        for status in ('created', 'disabled', 'unavailable', 'failed'):
            (self.ws / 'old.txt').write_bytes(b'before')
            async def snap(*args, outcome=None, **kwargs):
                self.assertEqual((self.ws / 'old.txt').read_bytes(), b'before')
                outcome['status'] = status
            with self.subTest(status=status), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as mocked:
                r = await upload_files('sid', [file('old.txt', b'after'), file('another.txt')], user=None, overwrite=True)
                self.assertEqual(mocked.await_count, 1)
                self.assertEqual(r['checkpoint_status'], status)
                self.assertIn('notice' if status == 'created' else 'warning', r)
                self.assertEqual((self.ws / 'old.txt').read_bytes(), b'after')

    async def test_only_skipped_no_checkpoint(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            r = await upload_files('sid', [file('.hidden'), file('large', b'x' * (20 * 1024 * 1024 + 1))], user=None, overwrite=True)
        snap.assert_not_awaited()
        self.assertEqual(r['saved'], [])
        self.assertEqual(len(r['skipped']), 2)
        self.assertNotIn('checkpoint_status', r)

    async def test_exception_cancel_before_write(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock, side_effect=OSError('private')):
            with self.assertRaises(HTTPException) as e: await upload_files('sid', [file('old.txt')], user=None, overwrite=True)
            self.assertNotIn('private', str(e.exception.detail))
        entered = asyncio.Event()
        async def snap(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            task = asyncio.create_task(upload_files('sid', [file('old.txt')], user=None, overwrite=True))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual((self.ws / 'old.txt').read_bytes(), b'before')

    async def test_workspace_changed_during_checkpoint(self):
        other = self.root / 'other'
        other.mkdir()
        with patch('server.main._ws', side_effect=[self.ws, other]), patch('engine.checkpoints.snapshot', new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as e: await upload_files('sid', [file('old.txt')], user=None, overwrite=True)
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual((self.ws / 'old.txt').read_bytes(), b'before')

    @unittest.skipUnless(os.name == 'posix', 'requires symlinks')
    async def test_symlink_escape_during_checkpoint(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'outside')
        async def snap(*args, **kwargs):
            (self.ws / 'old.txt').unlink()
            (self.ws / 'old.txt').symlink_to(outside)
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            with self.assertRaises(HTTPException): await upload_files('sid', [file('old.txt')], user=None, overwrite=True)
        self.assertEqual(outside.read_bytes(), b'outside')

    async def test_second_file_read_await_workspace_change(self):
        other = self.root / 'other'
        other.mkdir()
        class SwitchingUpload(UploadFile):
            async def read(self, size=-1):
                state[0] = other
                return await super().read(size)
        state = [self.ws]
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', side_effect=lambda *args: state[0]), patch('engine.checkpoints.snapshot', side_effect=snap):
            with self.assertRaises(HTTPException) as e:
                await upload_files('sid', [file('first.txt'), SwitchingUpload(filename='second.txt', file=io.BytesIO(b'bad'))], user=None, overwrite=True)
        self.assertEqual(e.exception.status_code, 409)
        self.assertTrue((self.ws / 'first.txt').exists())
        self.assertFalse((self.ws / 'second.txt').exists())
