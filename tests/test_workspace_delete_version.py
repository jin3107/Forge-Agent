"""Best-effort version preconditions for editor-initiated deletion."""
import hashlib
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from server.main import delete_workspace_file, read_workspace_file

def digest(data): return hashlib.sha256(data).hexdigest()

class DeleteVersion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.file = self.ws / 'file.bin'
        self.file.write_bytes(b'a\0b')

    async def test_binary_preview_and_matching_delete(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            preview = await read_workspace_file('s', 'file.bin', user=None)
            self.assertTrue(preview['binary'])
            self.assertEqual(preview['version'], digest(b'a\0b'))
            result = await delete_workspace_file('s', 'file.bin', user=None, expected_version=preview['version'])
        self.assertEqual(result['checkpoint_status'], 'created')
        self.assertFalse(self.file.exists())

    async def test_changed_before_or_during_snapshot_is_not_deleted(self):
        async def snap(*args, outcome=None, **kwargs):
            self.file.write_bytes(b'changed during snapshot')
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as mock:
            with self.assertRaises(HTTPException) as caught:
                await delete_workspace_file('s', 'file.bin', user=None, expected_version=digest(b'old'))
            self.assertEqual(caught.exception.status_code, 409)
            mock.assert_not_awaited()
            self.assertEqual(self.file.read_bytes(), b'a\0b')
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            with self.assertRaises(HTTPException) as caught:
                await delete_workspace_file('s', 'file.bin', user=None, expected_version=digest(b'a\0b'))
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.file.read_bytes(), b'changed during snapshot')

    async def test_missing_oversize_and_unreadable_conflict(self):
        for scenario in ('missing', 'oversize', 'unreadable'):
            self.file.write_bytes(b'x' * (4 * 1024 * 1024 + 1) if scenario == 'oversize' else b'a\0b')
            if scenario == 'missing': self.file.unlink()
            with self.subTest(scenario=scenario), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
                with patch.object(Path, 'open', side_effect=PermissionError('denied')) if scenario == 'unreadable' else nullcontext():
                    with self.assertRaises(HTTPException) as caught:
                        await delete_workspace_file('s', 'file.bin', user=None, expected_version=digest(b'a\0b'))
                self.assertEqual(caught.exception.status_code, 409)
                snap.assert_not_awaited()

    async def test_legacy_delete_without_version(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            result = await delete_workspace_file('s', 'file.bin', user=None)
        self.assertTrue(result['ok'])
        self.assertFalse(self.file.exists())
