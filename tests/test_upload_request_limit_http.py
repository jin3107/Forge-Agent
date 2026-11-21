"""Live HTTP checks for upload body limits, auth and chunked transfer."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests/fixtures/http_upload_limit_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'
CAP = 65536

def multipart(size):
    head = b'--fixture\r\nContent-Disposition: form-data; name="files"; filename="boundary.txt"\r\nContent-Type: application/octet-stream\r\n\r\n'
    tail = b'\r\n--fixture--\r\n'
    return head + b'x' * (size - len(head) - len(tail)) + tail

class UploadRequestLimitHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_length_chunked_auth_and_no_writes_on_rejection(self):
        with tempfile.TemporaryDirectory(prefix='forge-request-limit-') as td:
            root = Path(td)
            env = os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL'): env.pop(key, None)
            env.update(FORGE_DATA_DIR=str(root/'data'), PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            with (root/'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(sys.executable, '-u', str(HARNESS), str(root), cwd=str(ROOT), env=env, stdout=log, stderr=log)
                try:
                    async def ready():
                        while True:
                            if server.returncode is not None: raise AssertionError((root/'server.log').read_text())
                            if (root/'port').exists():
                                base = f'http://127.0.0.1:{(root/"port").read_text()}'
                                try:
                                    async with httpx.AsyncClient(trust_env=False, timeout=.5) as c:
                                        if (await c.get(base+'/api/health')).status_code == 200: return base
                                except httpx.HTTPError: pass
                            await asyncio.sleep(.03)
                    base = await asyncio.wait_for(ready(), 15)
                    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as c:
                        token = (await c.get('/api/auth/me')).json()['csrf_token']
                        c.headers['X-CSRF-Token'] = token
                        r = await c.post('/api/workspace/upload?sid=none', content=multipart(CAP+1), headers={'Content-Type':'multipart/form-data; boundary=fixture'})
                        self.assertEqual(r.status_code, 401, r.text)
                        r = await c.post('/api/auth/login', json={'email':'alice@example.test', 'password':PASSWORD})
                        self.assertEqual(r.status_code, 200, r.text)
                        c.headers['X-CSRF-Token'] = r.json()['csrf_token']
                        sid = (await c.post('/api/sessions', json={})).json()['session']['id']
                        path = f'/api/workspace/upload?sid={sid}'
                        headers = {'Content-Type':'multipart/form-data; boundary=fixture'}
                        r = await c.post(path, content=multipart(CAP+1), headers={**headers,'X-CSRF-Token':'wrong'})
                        self.assertEqual(r.status_code, 403, r.text)
                        r = await c.post(path, content=multipart(CAP+1), headers={**headers, 'Origin':'http://fixture.example'})
                        self.assertEqual(r.status_code, 413, r.text)
                        self.assertIn(r.headers.get('access-control-allow-origin'), ('*', 'http://fixture.example'))
                        self.assertEqual(r.headers.get('x-content-type-options'), 'nosniff')
                        async def stream(payload):
                            for i in range(0, len(payload), 4096): yield payload[i:i+4096]
                        r = await c.post(path, content=stream(multipart(CAP+1)), headers=headers)
                        self.assertEqual(r.status_code, 413, r.text)
                        self.assertEqual((await c.get('/api/workspace/file', params={'sid':sid, 'path':'boundary.txt'})).status_code, 404)
                        r = await c.post(path, content=stream(multipart(CAP)), headers=headers)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['saved'], ['boundary.txt'])
                        r = await c.get('/api/workspace/file', params={'sid':sid, 'path':'boundary.txt'})
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(len(r.json()['content']), CAP - len(multipart(0)))
                finally:
                    if server.returncode is None:
                        server.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(server.wait(), 10)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()
