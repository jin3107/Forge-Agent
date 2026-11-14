"""Tests for fetch_url redirect and DNS boundaries using only fake DNS/HTTP."""
import atexit
import asyncio
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

_tmp = tempfile.TemporaryDirectory(prefix="forge-fetch-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine.tools import _fetch_url  # noqa: E402


class FetchSecurity(unittest.IsolatedAsyncioTestCase):
    def dns(self, host, port, *args, **kwargs):
        ip = {"public.example": "93.184.216.34", "second.example": "93.184.216.35",
              "mixed.example": "93.184.216.34", "127.0.0.1": "127.0.0.1",
              "ipv6.example": "2606:4700:4700::1111", "::1": "::1"}.get(host)
        if ip is None:
            raise socket.gaierror("unknown test host")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        addr = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
        rows = [(family, socket.SOCK_STREAM, 6, "", addr)]
        if host == "mixed.example":
            rows.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port or 0)))
        return rows

    async def run_fetch(self, url, handler):
        original_client = httpx.AsyncClient
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)
        with patch("socket.getaddrinfo", side_effect=self.dns), patch("httpx.AsyncClient", side_effect=factory):
            return await _fetch_url(None, {"url": url})

    async def test_redirect_to_private_is_blocked_before_second_request(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})
            return httpx.Response(200, text="private marker")
        result = await self.run_fetch("http://public.example/start", handler)
        self.assertIn("SSRF", result)
        self.assertEqual(len(seen), 1, seen)
        self.assertNotIn("private marker", result)

    async def test_connection_uses_validated_ip_and_preserves_host_and_tls_name(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")
        result = await self.run_fetch("https://public.example:8443/path?q=1", handler)
        self.assertIn("[200]", result)
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["host"], "public.example:8443")
        self.assertEqual(seen[0].extensions["sni_hostname"], "public.example")
        self.assertIn("https://public.example:8443/path?q=1", result)

    async def test_mixed_public_private_dns_is_rejected_before_request(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="wrong")
        result = await self.run_fetch("https://mixed.example/", handler)
        self.assertIn("SSRF", result)
        self.assertFalse(seen)

    async def test_public_relative_redirect_is_revalidated(self):
        seen = []
        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": "https://second.example/new"})
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="safe")
        result = await self.run_fetch("https://public.example/old", handler)
        self.assertIn("safe", result)
        self.assertEqual([r.url.host for r in seen], ["93.184.216.34", "93.184.216.35"])
        self.assertEqual(seen[1].headers["host"], "second.example")

    async def test_ipv6_public_pinned_and_private_blocked(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="safe")
        result = await self.run_fetch("https://ipv6.example/", handler)
        self.assertIn("safe", result)
        self.assertEqual(seen[0].url.host, "2606:4700:4700::1111")
        self.assertEqual(seen[0].headers["host"], "ipv6.example")
        seen.clear()
        result = await self.run_fetch("http://[::1]/", handler)
        self.assertIn("SSRF", result)
        self.assertFalse(seen)

    async def test_redirect_loop_is_bounded(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "/again"})
        result = await self.run_fetch("https://public.example/", handler)
        self.assertTrue(result.startswith("Error"), result)
        self.assertLessEqual(len(seen), 6)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for the local TLS test")
    async def test_real_local_tls_preserves_hostname(self):
        # Deliberately replace the public-IP resolver with loopback solely to
        # test a real local TLS connection; SSRF policy is covered separately.
        with tempfile.TemporaryDirectory(prefix="forge-fetch-tls-") as td:
            cert, key = Path(td) / "cert.pem", Path(td) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=public.example", "-addext", "subjectAltName=DNS:public.example"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(cert, key)
            sni = []
            server_ctx.set_servername_callback(lambda sock, name, ctx: sni.append(name))
            hosts = []
            async def handle(reader, writer):
                try:
                    data = await reader.readuntil(b"\r\n\r\n")
                    hosts.append(data.decode("ascii"))
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 8\r\nConnection: close\r\n\r\ntls good")
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
            port = server.sockets[0].getsockname()[1]
            original = httpx.AsyncClient
            client_ctx = ssl.create_default_context(cafile=str(cert))
            def factory(*args, **kwargs):
                kwargs["verify"] = client_ctx
                return original(*args, **kwargs)
            try:
                with patch("engine.tools._public_addresses", return_value=["127.0.0.1"]), \
                     patch("httpx.AsyncClient", side_effect=factory):
                    result = await _fetch_url(None, {"url": f"https://public.example:{port}/test"})
            finally:
                server.close()
                await server.wait_closed()
            self.assertIn("tls good", result)
            self.assertEqual(sni, ["public.example"])
            self.assertIn(f"Host: public.example:{port}", hosts[0])

    async def test_url_credentials_rejected(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200)
        result = await self.run_fetch("https://user:password@public.example/", handler)
        self.assertTrue(result.startswith("Error"), result)
        self.assertFalse(seen)


if __name__ == "__main__":
    unittest.main()
