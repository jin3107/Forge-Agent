"""Multipart overflow must close spooled files, including older Starlette."""
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI, File, UploadFile
import starlette.formparsers as forms
from server.upload_limit import UploadRequestLimit

class UploadLimitSpoolCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_stream_overflow_closes_rolled_spool_before_response(self):
        cap = 2 * 1024 * 1024
        app = FastAPI()
        calls = []
        @app.post('/api/workspace/upload')
        async def endpoint(files: list[UploadFile] = File(...)):
            calls.append(True)
            return {'ok':True}
        app.add_middleware(UploadRequestLimit, max_bytes=cap)
        original = forms.SpooledTemporaryFile
        made = []
        def record(*args, **kwargs):
            f = original(*args, **kwargs)
            made.append(f)
            return f
        head = b'--fixture\r\nContent-Disposition: form-data; name="files"; filename="data.bin"\r\n\r\n'
        async def body():
            yield head
            # Allow the multipart parser to spool >1 MiB before rejection.
            for _ in range(33): yield b'x' * 65536
            yield b'\r\n--fixture--\r\n'
        with patch.object(forms, 'SpooledTemporaryFile', side_effect=record):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/api/workspace/upload', content=body(), headers={'content-type':'multipart/form-data; boundary=fixture'})
        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(calls)
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0]._rolled, 'fixture must exercise a disk-backed spool')
        self.assertTrue(made[0].closed, 'parser must close the temporary file on rejection')
