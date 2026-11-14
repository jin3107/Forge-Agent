"""Preview must not silently replace invalid UTF-8 bytes in editable content."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server.main import read_workspace_file

class WorkspacePreviewEncoding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-preview-encoding-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def test_invalid_utf8_and_late_nul_are_read_only(self):
        samples = {
            'latin1.txt': b'caf\xe9',
            'bad-sequence.txt': b'hello\xffworld',
            'incomplete.txt': b'end\xe2\x82',
            'late-nul.bin': b'a' * 2000 + b'\0end',
            'utf16.txt': b'\xff\xfec\x00',
        }
        with patch('server.main._ws', return_value=self.ws):
            for name, data in samples.items():
                with self.subTest(name=name):
                    (self.ws/name).write_bytes(data)
                    result = await read_workspace_file('sid', name, user=None)
                    self.assertEqual(result, {'path':name, 'binary':True, 'content':'', 'version':hashlib.sha256(data).hexdigest()})
                    self.assertEqual((self.ws/name).read_bytes(),data)

    async def test_valid_utf8_unicode_and_replacement_character_preserved(self):
        cases = ['Việt Nam \U0001f30f\n', '\ufeffBOM\r\n', 'valid literal \ufffd', '', 'tabs\tline\n']
        with patch('server.main._ws', return_value=self.ws):
            for n, text in enumerate(cases):
                with self.subTest(text=text):
                    name = f'{n}.txt'
                    (self.ws/name).write_bytes(text.encode('utf-8'))
                    result = await read_workspace_file('sid', name, user=None)
                    self.assertEqual(result['path'], name)
                    self.assertFalse(result['binary'])
                    self.assertEqual(result['content'], text)
                    self.assertEqual(len(result['version']), 64)
