"""ASGI upload body cap, including chunked/no Content-Length streams."""
import unittest
from starlette.formparsers import MultiPartException

class UploadRequestLimitTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, chunks, headers=(), path='/api/workspace/upload', method='POST', cap=8):
        from server.upload_limit import UploadRequestLimit
        sent = []
        called = []
        remaining = list(chunks)
        async def receive():
            return remaining.pop(0) if remaining else {'type':'http.disconnect'}
        async def send(message): sent.append(message)
        async def app(scope, read, write):
            called.append(True)
            try:
                while True:
                    msg = await read()
                    if msg['type'] != 'http.request' or not msg.get('more_body', False): break
                status = 200
            except MultiPartException:
                status = 400  # Starlette maps multipart parse errors to 400.
            await write({'type':'http.response.start', 'status':status, 'headers':[]})
            await write({'type':'http.response.body', 'body':b''})
        middleware = UploadRequestLimit(app, max_bytes=cap)
        await middleware({'type':'http','path':path,'method':method,'headers':list(headers)}, receive, send)
        return sent[0]['status'], bool(called), remaining
    async def test_content_length_rejected_before_receive(self):
        status, called, remaining = await self.run_case([{'type':'http.request','body':b'x'}], [(b'content-length',b'9')])
        self.assertEqual(status, 413)
        self.assertFalse(called)
        self.assertEqual(len(remaining), 1)
    async def test_stream_cap_and_exact_boundary(self):
        def msg(b, more): return {'type':'http.request','body':b,'more_body':more}
        self.assertEqual((await self.run_case([msg(b'1234', True), msg(b'5678', False)]))[0], 200)
        self.assertEqual((await self.run_case([msg(b'1234', True), msg(b'56789', False)]))[0], 413)
    async def test_small_or_invalid_declared_length_still_counts_stream(self):
        chunk = [{'type':'http.request','body':b'x' * 9}]
        for value in (b'1', b'invalid'):
            with self.subTest(value=value):
                status, called, _ = await self.run_case(chunk, [(b'content-length', value)])
                self.assertTrue(called)
                self.assertEqual(status, 413)
    async def test_other_paths_and_methods_are_unaffected(self):
        chunk = [{'type':'http.request','body':b'x' * 9}]
        self.assertEqual((await self.run_case(chunk, path='/api/other'))[0], 200)
        self.assertEqual((await self.run_case(chunk, method='GET'))[0], 200)
    async def test_disconnect_propagates(self):
        status, called, _ = await self.run_case([{'type':'http.disconnect'}])
        self.assertEqual(status, 200)
        self.assertTrue(called)
