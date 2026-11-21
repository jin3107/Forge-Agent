"""Workspace file preview reads a bounded amount without returning truncated text."""
import io
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from server.main import read_workspace_file

LIMIT = 4 * 1024 * 1024

class WorkspaceReadLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-read-limit-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    async def test_exact_limit_and_over_limit(self):
        (self.ws / 'exact.txt').write_bytes(b'x' * LIMIT)
        (self.ws / 'over.txt').write_bytes(b'x' * (LIMIT + 1))
        with patch('server.main._ws', return_value=self.ws):
            result = await read_workspace_file('sid', 'exact.txt', user=None)
            self.assertFalse(result['binary'])
            self.assertEqual(len(result['content']), LIMIT)
            with self.assertRaises(HTTPException) as e: await read_workspace_file('sid', 'over.txt', user=None)
        self.assertEqual(e.exception.status_code, 413)
        self.assertIn('large', e.exception.detail.lower())

    async def test_read_requests_at_most_limit_plus_one(self):
        target = self.ws / 'big.txt'
        target.write_bytes(b'exists')
        class Spy(io.BytesIO):
            requests = []
            def read(self, size=-1):
                self.requests.append(size)
                return super().read(size)
        stream = Spy(b'z' * (LIMIT + 1024))
        with patch('server.main._ws', return_value=self.ws), patch.object(Path, 'open', return_value=stream):
            with self.assertRaises(HTTPException) as e: await read_workspace_file('sid', 'big.txt', user=None)
        self.assertEqual(e.exception.status_code, 413)
        self.assertEqual(stream.requests, [LIMIT + 1])
        self.assertTrue(stream.closed)

    async def test_binary_and_invalid_utf8_are_not_editable(self):
        (self.ws / 'bin').write_bytes(b'abc\x00xyz')
        (self.ws / 'text').write_bytes(b'hello\xffworld')
        with patch('server.main._ws', return_value=self.ws):
            self.assertEqual(await read_workspace_file('sid', 'bin', user=None), {'path':'bin','binary':True,'content':'','version':hashlib.sha256(b'abc\x00xyz').hexdigest()})
            self.assertEqual(await read_workspace_file('sid', 'text', user=None), {'path':'text','binary':True,'content':'','version':hashlib.sha256(b'hello\xffworld').hexdigest()})

    async def test_missing_directory_escape_and_binary_oversize(self):
        (self.ws / 'large.bin').write_bytes(b'\x00' + b'x' * LIMIT)
        with patch('server.main._ws', return_value=self.ws):
            for path, code in [('missing',404), ('.',404), ('../outside',400), ('large.bin',413)]:
                with self.subTest(path=path):
                    with self.assertRaises(HTTPException) as e: await read_workspace_file('sid', path, user=None)
                    self.assertEqual(e.exception.status_code, code)
