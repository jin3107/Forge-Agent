"""Conditional saves must remain within the editable preview byte limit."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from server.main import FileSaveBody, read_workspace_file, save_workspace_file

LIMIT = 4 * 1024 * 1024

class ConditionalSaveSize(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.path = self.ws / 'file.txt'
        self.path.write_bytes(b'initial')
        self.version = hashlib.sha256(b'initial').hexdigest()

    async def test_exact_limit_and_multibyte_reopen(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        text = 'é' * (LIMIT // 2)
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            result = await save_workspace_file('s', FileSaveBody(path='file.txt', content=text, expected_version=self.version), user=None)
            preview = await read_workspace_file('s', 'file.txt', user=None)
        self.assertEqual(preview['content'], text)
        self.assertEqual(preview['version'], result['version'])
        self.assertEqual(self.path.stat().st_size, LIMIT)

    async def test_oversize_refused_before_checkpoint_preserves_file(self):
        for text in ('a' * (LIMIT + 1), 'é' * (LIMIT // 2 + 1), 'a\r\n' * (LIMIT // 3 + 1)):
            self.path.write_bytes(b'initial')
            with self.subTest(length=len(text)), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
                with self.assertRaises(HTTPException) as caught:
                    await save_workspace_file('s', FileSaveBody(path='file.txt', content=text, expected_version=self.version), user=None)
                self.assertEqual(caught.exception.status_code, 413)
                snap.assert_not_awaited()
                self.assertEqual(self.path.read_bytes(), b'initial')

    async def test_invalid_unicode_fails_before_checkpoint(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            with self.assertRaises(HTTPException) as caught:
                await save_workspace_file('s', FileSaveBody(path='file.txt', content='\ud800', expected_version=self.version), user=None)
            self.assertEqual(caught.exception.status_code, 422)
            snap.assert_not_awaited()
            self.assertEqual(self.path.read_bytes(), b'initial')

    async def test_legacy_oversize_save_unchanged(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'failed'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            result = await save_workspace_file('s', FileSaveBody(path='file.txt', content='x' * (LIMIT + 1)), user=None)
        self.assertTrue(result['ok'])
        self.assertEqual(self.path.stat().st_size, LIMIT + 1)
