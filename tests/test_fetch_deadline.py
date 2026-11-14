"""Overall fetch deadline regressions (local/mock only)."""
import asyncio
import time
import unittest
from unittest.mock import patch

import httpx

from engine import tools


class SlowStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        while True:
            await asyncio.sleep(0.02)
            yield b'x'

    async def aclose(self):
        self.closed = True


class FetchDeadline(unittest.IsolatedAsyncioTestCase):
    async def run_mock(self, handler, limit=0.12):
        original = httpx.AsyncClient
        def factory(*args, **kwargs):
            kwargs['transport'] = httpx.MockTransport(handler)
            return original(*args, **kwargs)
        with patch.object(tools, 'FETCH_TOTAL_TIMEOUT', limit, create=True), \
             patch('engine.tools._public_addresses', return_value=['93.184.216.34']), \
             patch('httpx.AsyncClient', side_effect=factory):
            return await asyncio.wait_for(tools._fetch_url(None, {'url': 'https://example.com/'}), 0.8)

    async def test_slow_body_deadline_closes_stream(self):
        stream = SlowStream()
        result = await self.run_mock(lambda r: httpx.Response(200, stream=stream))
        self.assertTrue(result.startswith('Error'), result)
        self.assertIn('timeout', result.lower())
        self.assertTrue(stream.closed)

    async def test_deadline_is_shared_across_redirects(self):
        calls = []
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0.07)
            return httpx.Response(302, headers={'location': '/next'})
        result = await self.run_mock(handler)
        self.assertIn('timeout', result.lower())
        self.assertGreaterEqual(len(calls), 2)
        self.assertLess(len(calls), 6)

    async def test_dns_is_inside_total_deadline(self):
        def slow_dns(*args):
            time.sleep(0.5)
            return ['93.184.216.34']
        original = httpx.AsyncClient
        def factory(*args, **kwargs):
            kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200, text='unexpected'))
            return original(*args, **kwargs)
        with patch.object(tools, 'FETCH_TOTAL_TIMEOUT', 0.1, create=True), \
             patch('engine.tools._public_addresses', side_effect=slow_dns), \
             patch('httpx.AsyncClient', side_effect=factory):
            result = await asyncio.wait_for(tools._fetch_url(None, {'url': 'https://example.com/'}), 0.8)
        self.assertIn('timeout', result.lower())

    async def test_caller_cancellation_propagates_and_closes(self):
        stream = SlowStream()
        task = asyncio.create_task(self.run_mock(lambda r: httpx.Response(200, stream=stream), limit=10))
        await asyncio.wait_for(stream.started.wait(), 0.8)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)

    async def test_real_local_slow_body_disconnects(self):
        disconnected = asyncio.Event()
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 10000\r\n\r\n')
                await writer.drain()
                while True:
                    writer.write(b'x')
                    await writer.drain()
                    await asyncio.sleep(0.02)
            except (ConnectionError, asyncio.IncompleteReadError):
                disconnected.set()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with patch.object(tools, 'FETCH_TOTAL_TIMEOUT', 0.15, create=True), \
                 patch('engine.tools._public_addresses', return_value=['127.0.0.1']):
                result = await asyncio.wait_for(tools._fetch_url(None, {'url': f'http://example.com:{port}/'}), 0.9)
                self.assertIn('timeout', result.lower())
                await asyncio.wait_for(disconnected.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
