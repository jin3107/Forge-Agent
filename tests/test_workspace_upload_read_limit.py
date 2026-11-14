"""Upload endpoint bounds its file read; this is not a multipart/request cap."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import UploadFile
from server.main import upload_files

LIMIT = 20 * 1024 * 1024

class ObservedUpload(UploadFile):
    def __init__(self, name, size):
        super().__init__(filename=name, file=io.BytesIO(b'x' * size))
        self.requests = []
    async def read(self, size=-1):
        self.requests.append(size)
        return await super().read(size)

class WorkspaceUploadReadLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-upload-limit-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
    async def test_exact_limit_and_over_limit(self):
        exact = ObservedUpload('exact.bin', LIMIT)
        over = ObservedUpload('over.bin', LIMIT + 1024 * 1024)
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as checkpoint:
            r = await upload_files('sid', [exact, over], user=None)
        self.assertEqual(exact.requests, [LIMIT + 1])
        self.assertEqual(over.requests, [LIMIT + 1])
        self.assertEqual(over.file.tell(), LIMIT + 1)
        self.assertEqual(r['saved'], ['exact.bin'])
        self.assertEqual(r['skipped'], ['over.bin (>20MB)'])
        self.assertEqual((self.ws / 'exact.bin').stat().st_size, LIMIT)
        self.assertFalse((self.ws / 'over.bin').exists())
        self.assertEqual(checkpoint.await_count, 1)
    async def test_oversize_first_and_hidden_do_not_checkpoint_until_valid(self):
        over = ObservedUpload('over.bin', LIMIT + 1)
        hidden = ObservedUpload('.hidden', 4)
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as checkpoint:
            r = await upload_files('sid', [over, hidden], user=None)
        self.assertEqual(over.requests, [LIMIT + 1])
        self.assertEqual(hidden.requests, [])
        self.assertEqual(r['saved'], [])
        self.assertNotIn('checkpoint_status', r)
        checkpoint.assert_not_awaited()
    async def test_small_after_oversize_is_saved_once(self):
        over = ObservedUpload('over.bin', LIMIT + 1)
        small = ObservedUpload('small.bin', 5)
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'failed'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as checkpoint:
            r = await upload_files('sid', [over, small], user=None)
        self.assertEqual(r['saved'], ['small.bin'])
        self.assertEqual(r['skipped'], ['over.bin (>20MB)'])
        self.assertEqual(r['checkpoint_status'], 'failed')
        self.assertIn('undo', r['warning'].lower())
        self.assertEqual((self.ws / 'small.bin').read_bytes(), b'xxxxx')
        self.assertEqual(checkpoint.await_count, 1)
