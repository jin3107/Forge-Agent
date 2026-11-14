"""Best-effort content-version preconditions for workspace editor saves."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from pydantic import ValidationError
from server.main import FileSaveBody, read_workspace_file, save_workspace_file

def digest(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

class WorkspaceSaveVersion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-save-version-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.target = self.ws/'file.txt'
        self.target.write_bytes(b'before\r\n')
    async def test_preview_and_matching_save_return_exact_byte_hash(self):
        async def snapshot(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            r = await read_workspace_file('s','file.txt',user=None)
            self.assertEqual(r['version'],digest(b'before\r\n'))
            saved = await save_workspace_file('s',FileSaveBody(path='file.txt',content='after\r\n',expected_version=r['version']),user=None)
        self.assertEqual(saved['version'],digest(b'after\r\n'))
        self.assertEqual(self.target.read_bytes(),b'after\r\n')
    async def test_changed_before_snapshot_rejected_without_snapshot(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            with self.assertRaises(HTTPException) as e:
                await save_workspace_file('s',FileSaveBody(path='file.txt',content='mine',expected_version=digest(b'old')),user=None)
        self.assertEqual(e.exception.status_code,409)
        self.assertIn('changed',str(e.exception.detail).lower())
        self.assertEqual(self.target.read_bytes(),b'before\r\n')
        snap.assert_not_awaited()
    async def test_changed_during_snapshot_rejected(self):
        async def snapshot(*args, outcome=None, **kwargs):
            self.target.write_bytes(b'other writer')
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snapshot):
            with self.assertRaises(HTTPException) as e:
                await save_workspace_file('s',FileSaveBody(path='file.txt',content='mine',expected_version=digest(b'before\r\n')),user=None)
        self.assertEqual(e.exception.status_code,409)
        self.assertEqual(self.target.read_bytes(),b'other writer')
    async def test_deleted_or_oversize_current_file_rejected(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            for kind in ('deleted','oversize'):
                if kind == 'deleted': self.target.unlink()
                else: self.target.write_bytes(b'x'*(4*1024*1024+1))
                with self.subTest(kind=kind), self.assertRaises(HTTPException) as e:
                    await save_workspace_file('s',FileSaveBody(path='file.txt',content='mine',expected_version=digest(b'before\r\n')),user=None)
                self.assertEqual(e.exception.status_code,409)
            snap.assert_not_awaited()
    async def test_unreadable_current_file_fails_closed(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap, patch.object(Path, 'open', side_effect=PermissionError('denied')):
            with self.assertRaises(HTTPException) as e:
                await save_workspace_file('s', FileSaveBody(path='file.txt', content='mine', expected_version=digest(b'before\r\n')), user=None)
            self.assertEqual(e.exception.status_code, 409)
            snap.assert_not_awaited()
        self.assertEqual(self.target.read_bytes(), b'before\r\n')
    async def test_legacy_without_version_still_overwrites(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status']='failed'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            r = await save_workspace_file('s', FileSaveBody(path='file.txt',content='legacy'), user=None)
        self.assertEqual(self.target.read_bytes(),b'legacy')
        self.assertEqual(r['checkpoint_status'],'failed')
    async def test_invalid_hash_is_validation_error(self):
        for bad in ('', 'ABC', '0'*63, 'g'*64, 'A'*64):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                FileSaveBody(path='file.txt',content='x',expected_version=bad)
