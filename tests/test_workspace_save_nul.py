"""Do not write NUL-containing data through the versioned text editor."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from server.main import FileSaveBody, read_workspace_file, save_workspace_file

class ConditionalSaveNul(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.path = self.ws / 'file.txt'
        self.path.write_bytes(b'original')
        self.version = hashlib.sha256(b'original').hexdigest()

    async def test_reject_nul_before_checkpoint_without_changing_disk(self):
        for text in ('\0', 'safe\0suffix', 'x' * 2048 + '\0'):
            self.path.write_bytes(b'original')
            with self.subTest(text_length=len(text)), patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
                with self.assertRaises(HTTPException) as caught:
                    await save_workspace_file('s', FileSaveBody(path='file.txt', content=text, expected_version=self.version), user=None)
                self.assertEqual(caught.exception.status_code, 422)
                snap.assert_not_awaited()
                self.assertEqual(self.path.read_bytes(), b'original')

    async def test_valid_controls_and_replacement_character_are_preserved(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        text = '\tline\n\ufffd\x01'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            saved = await save_workspace_file('s', FileSaveBody(path='file.txt', content=text, expected_version=self.version), user=None)
            preview = await read_workspace_file('s', 'file.txt', user=None)
        self.assertEqual(preview['content'], text)
        self.assertEqual(preview['version'], saved['version'])

    async def test_legacy_put_keeps_old_behavior(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'failed'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            result = await save_workspace_file('s', FileSaveBody(path='file.txt', content='a\0b'), user=None)
        self.assertTrue(result['ok'])
        self.assertEqual(self.path.read_bytes(), b'a\0b')
