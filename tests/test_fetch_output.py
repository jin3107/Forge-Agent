"""Bounded fetch response regressions; fake streaming transport, no public network."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from engine import tools


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class FetchOutput(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler):
        original = httpx.AsyncClient
        def factory(*args, **kwargs):
            kwargs['transport'] = httpx.MockTransport(handler)
            return original(*args, **kwargs)
        with patch('engine.tools._public_addresses', return_value=['93.184.216.34']), \
             patch('httpx.AsyncClient', side_effect=factory):
            return await tools._fetch_url(None, {'url': 'https://example.com/'})

    async def test_large_stream_stops_early_and_closes(self):
        stream = CountingStream([b'x' * 4096] * 2048)
        result = await self.fetch(lambda r: httpx.Response(200, headers={'content-type': 'text/plain'}, stream=stream))
        self.assertLess(stream.reads, 2048)
        self.assertTrue(stream.closed)
        self.assertIn('cắt', result)
        self.assertLess(len(result), 21000)

    async def test_redirect_body_not_consumed(self):
        redirect = CountingStream([b'x' * 4096] * 2048)
        def handler(request):
            if request.url.path == '/':
                return httpx.Response(302, headers={'location': '/next'}, stream=redirect)
            return httpx.Response(200, text='safe')
        result = await self.fetch(handler)
        self.assertIn('safe', result)
        self.assertEqual(redirect.reads, 0)
        self.assertTrue(redirect.closed)

    async def test_binary_body_not_consumed(self):
        stream = CountingStream([b'x' * 10000] * 100)
        result = await self.fetch(lambda r: httpx.Response(200, headers={'content-type': 'application/pdf'}, stream=stream))
        self.assertTrue(result.startswith('Error'))
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_compressed_body_rejected_before_decompression(self):
        stream = CountingStream([b'x' * 1024] * 100)
        result = await self.fetch(lambda r: httpx.Response(200, headers={'content-type': 'text/plain', 'content-encoding': 'gzip'}, stream=stream))
        self.assertTrue(result.startswith('Error'), result)
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_short_content_charset_and_accept_encoding(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type': 'text/plain; charset=iso-8859-1'}, content=b'caf\xe9')
        result = await self.fetch(handler)
        self.assertIn('café', result)
        self.assertEqual(requests[0].headers['accept-encoding'], 'identity')

    async def test_utf8_at_input_boundary_and_html(self):
        cap = tools.MAX_FETCH_BYTES
        stream = CountingStream([b'<p>' + b'x' * (cap - 4) + bytes([195]), bytes([169]) + b'</p>'])
        result = await self.fetch(lambda r: httpx.Response(200, headers={'content-type': 'text/html'}, stream=stream))
        self.assertIn('giới hạn tải', result)
        self.assertNotIn('�', result)
        self.assertNotIn('<p>', result)

    async def test_real_local_http_large_response(self):
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                body = b'z' * (tools.MAX_FETCH_BYTES + 10000)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with patch('engine.tools._public_addresses', return_value=['127.0.0.1']):
                result = await tools._fetch_url(None, {'url': f'http://example.com:{port}/'})
        finally:
            server.close()
            await server.wait_closed()
        self.assertIn('giới hạn tải', result)
        self.assertLess(len(result), 21000)

    async def test_exact_input_byte_limit_and_one_more(self):
        cap = tools.MAX_FETCH_BYTES
        for extra in (0, 1):
            stream = CountingStream([b'x' * cap, b'x' * extra])
            result = await self.fetch(lambda r: httpx.Response(200, headers={'content-type': 'text/plain'}, stream=stream))
            self.assertEqual('giới hạn tải' in result, bool(extra), result[-200:])
            self.assertTrue(stream.closed)


if __name__ == '__main__':
    unittest.main()
