"""Exclusive workspace create must not overwrite an existing path."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from engine import checkpoints
from server.main import FileSaveBody, create_workspace_file

class WorkspaceCreateFile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-create-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root/'ws'
        self.ws.mkdir()
        self.repo = self.root/'checkpoint.git'
    def body(self, path, content=''): return FileSaveBody(path=path, content=content)

    async def test_existing_file_rejected_before_snapshot(self):
        target = self.ws/'existing.bin'
        target.write_bytes(b'\0important')
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('existing.bin'), user=None)
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(target.read_bytes(), b'\0important')
        snap.assert_not_awaited()

    async def test_create_and_undo_real_git(self):
        if not checkpoints.available(): self.skipTest('git unavailable')
        with patch('server.main._ws', return_value=self.ws), patch.object(checkpoints, '_repo_dir', return_value=self.repo), patch('engine.config.load_config', return_value={'checkpoints_enabled':True}):
            result = await create_workspace_file('s', self.body('nested/new.txt','hello'), user=None)
            self.assertEqual(result['checkpoint_status'], 'created')
            self.assertIn('ignored', result['notice'].lower())
            self.assertEqual((self.ws/'nested/new.txt').read_text(), 'hello')
            r = await checkpoints.undo(checkpoints.key_for(self.ws), self.ws)
        self.assertTrue(r['ok'], r)
        self.assertFalse((self.ws/'nested/new.txt').exists())

    async def test_snapshot_status_warning_and_single_snapshot(self):
        for status in ('created','failed','disabled','unavailable'):
            async def snap(*args, outcome=None, **kwargs):
                self.assertFalse((self.ws/status).exists())
                outcome['status'] = status
            with self.subTest(status=status), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as mock:
                r = await create_workspace_file('s', self.body(status, status), user=None)
            self.assertEqual(mock.await_count, 1)
            self.assertEqual(r['checkpoint_status'], status)
            self.assertIn('notice' if status == 'created' else 'warning', r)
            self.assertEqual((self.ws/status).read_text(), status)

    async def test_invalid_and_directory_rejected_before_snapshot(self):
        for path, expected in [('../outside',400), ('.',400)]:
            with self.subTest(path=path), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
                with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body(path), user=None)
                self.assertEqual(e.exception.status_code, expected)
                snap.assert_not_awaited()

    async def test_race_create_after_snapshot_does_not_overwrite(self):
        async def snap(*args, outcome=None, **kwargs):
            (self.ws/'new.txt').write_text('concurrent')
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('new.txt','overwrite'), user=None)
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual((self.ws/'new.txt').read_text(), 'concurrent')

    async def test_created_immediately_before_exclusive_open_is_preserved(self):
        original_open = Path.open
        target = self.ws/'race.txt'
        def raced_open(path, mode='r', *args, **kwargs):
            if path == target and mode == 'x':
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f: f.write('winner')
            return original_open(path, mode, *args, **kwargs)
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap), patch.object(Path, 'open', raced_open):
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('race.txt','overwrite'), user=None)
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(target.read_text(), 'winner')

    async def test_two_concurrent_creates_only_one_wins(self):
        entered = 0
        both = asyncio.Event()
        async def snap(*args, outcome=None, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2: both.set()
            await asyncio.wait_for(both.wait(), 2)
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            r = await asyncio.gather(*(create_workspace_file('s', self.body('same.txt', str(n)), user=None) for n in (1,2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x,dict) for x in r), 1)
        error = next(x for x in r if isinstance(x,Exception))
        self.assertIsInstance(error, HTTPException)
        self.assertEqual(error.status_code, 409)
        self.assertIn((self.ws/'same.txt').read_text(), ('1','2'))

    async def test_failure_cancellation_and_workspace_change(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock, side_effect=OSError('secret')):
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('new'), user=None)
            self.assertNotIn('secret', str(e.exception.detail))
        entered = asyncio.Event()
        async def snap(*args, **kwargs): entered.set(); await asyncio.Event().wait()
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            t = asyncio.create_task(create_workspace_file('s', self.body('new'), user=None))
            await asyncio.wait_for(entered.wait(),2)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError): await t
        other = self.root/'other'; other.mkdir()
        with patch('server.main._ws', side_effect=[self.ws,other]), patch('engine.checkpoints.snapshot', new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('new'), user=None)
            self.assertEqual(e.exception.status_code,409)
        self.assertFalse((self.ws/'new').exists())

    @unittest.skipUnless(os.name == 'posix', 'symlinks required')
    async def test_existing_dangling_symlink_rejected(self):
        (self.ws/'link').symlink_to(self.ws/'missing')
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            with self.assertRaises(HTTPException) as e: await create_workspace_file('s', self.body('link','bad'), user=None)
        self.assertEqual(e.exception.status_code, 409)
        snap.assert_not_awaited()
        self.assertFalse((self.ws/'missing').exists())

    @unittest.skipUnless(os.name == 'posix', 'symlinks required')
    async def test_symlink_escape_during_snapshot(self):
        outside = self.root/'outside'; outside.write_text('safe')
        async def snap(*args, **kwargs): (self.ws/'new').symlink_to(outside)
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            with self.assertRaises(HTTPException): await create_workspace_file('s', self.body('new','bad'), user=None)
        self.assertEqual(outside.read_text(), 'safe')
