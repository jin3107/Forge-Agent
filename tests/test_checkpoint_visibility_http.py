"""Authenticated HTTP and SSE delivery of checkpoint warnings."""
import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests/fixtures/http_checkpoint_visibility_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'


class CheckpointVisibilityHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_and_agent_sse_include_failed_checkpoint_warning(self):
        with tempfile.TemporaryDirectory(prefix='forge-checkpoint-http-') as td:
            root = Path(td)
            env = os.environ.copy()
            for key in ('FORGE_PROFILE', 'FORGE_RELEASE_MODE', 'FORGE_COOKIE_SECURE', 'FORGE_PUBLIC_URL'):
                env.pop(key, None)
            env.update(FORGE_DATA_DIR=str(root / 'data'), PYTHONPATH=str(ROOT) + os.pathsep + env.get('PYTHONPATH', ''))
            with (root / 'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(sys.executable, '-u', str(HARNESS), str(root),
                    cwd=str(ROOT), env=env, stdout=log, stderr=log)
                try:
                    async def ready():
                        while True:
                            if server.returncode is not None:
                                raise AssertionError((root / 'server.log').read_text())
                            if (root / 'port').exists():
                                base = f'http://127.0.0.1:{(root / "port").read_text()}'
                                try:
                                    async with httpx.AsyncClient(trust_env=False, timeout=.5) as client:
                                        if (await client.get(base + '/api/health')).status_code == 200:
                                            return base
                                except httpx.HTTPError:
                                    pass
                            await asyncio.sleep(.03)
                    base = await asyncio.wait_for(ready(), 15)
                    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as client, \
                               httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as anon:
                        anon_me = await anon.get('/api/auth/me')
                        anon.headers['X-CSRF-Token'] = anon_me.json()['csrf_token']
                        self.assertEqual((await anon.post('/api/workspace/exec', params={'sid':'missing'},
                                                         json={'command':'echo hello'})).status_code, 401)
                        r = await client.get('/api/auth/me')
                        client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                        r = await client.post('/api/auth/login', json={'email': 'alice@example.test', 'password': PASSWORD})
                        self.assertEqual(r.status_code, 200, r.text)
                        client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                        r = await client.post('/api/sessions', json={})
                        self.assertEqual(r.status_code, 200, r.text)
                        sid = r.json()['session']['id']
                        r = await client.post('/api/workspace/file', params={'sid': sid},
                            json={'path': 'created.txt', 'content': 'created without checkpoint'})
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['checkpoint_status'], 'failed')
                        self.assertIn('undo', r.json()['warning'].lower())
                        r = await client.put('/api/workspace/file', params={'sid': sid},
                            json={'path': 'direct.txt', 'content': 'saved without checkpoint'})
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertTrue(r.json()['ok'])
                        self.assertEqual(r.json()['checkpoint_status'], 'failed')
                        self.assertIn('undo', r.json()['warning'].lower())
                        r = await client.get('/api/workspace/file', params={'sid': sid, 'path': 'direct.txt'})
                        self.assertEqual(r.json()['content'], 'saved without checkpoint')
                        r = await client.delete('/api/workspace/file', params={'sid': sid, 'path': 'direct.txt'})
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['checkpoint_status'], 'failed')
                        self.assertIn('undo', r.json()['warning'].lower())
                        self.assertEqual((await client.get('/api/workspace/file', params={'sid': sid, 'path': 'direct.txt'})).status_code, 404)
                        r = await client.post('/api/workspace/upload', params={'sid': sid}, files=[('files', ('uploaded.txt', b'uploaded')), ('files', ('.hidden', b'skipped'))])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['checkpoint_status'], 'failed')
                        self.assertIn('undo', r.json()['warning'].lower())
                        self.assertEqual(r.json()['saved'], ['uploaded.txt'])
                        self.assertEqual(r.json()['skipped'], ['.hidden'])
                        r = await client.post('/api/workspace/upload', params={'sid': sid},
                            files=[('files', (f'count-{i}.txt', b'x')) for i in range(1,22)])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(len(r.json()['saved']), 20)
                        self.assertEqual(r.json()['skipped'], ['#21: count-21.txt (over 20-file limit)'])
                        self.assertEqual(r.json()['checkpoint_status'], 'failed')
                        self.assertIn('undo', r.json()['warning'].lower())
                        r = await client.post('/api/workspace/exec', params={'sid': sid},
                            headers={'X-CSRF-Token':'wrong'}, json={'command':'echo fixture'})
                        self.assertEqual(r.status_code, 403, r.text)
                        r = await client.post('/api/workspace/exec', params={'sid': sid}, json={'command':'echo fixture'})
                        self.assertEqual(r.status_code, 200, r.text)
                        result = r.json()
                        self.assertTrue(result['ok'], result)
                        self.assertEqual(result['checkpoint_status'], 'failed')
                        self.assertIn('fixture', result['output'])
                        self.assertIn('Undo may not restore', result['output'])
                        request = {'name': 'write_file', 'args': {'path': 'http.txt', 'content': 'written'}}
                        r = await client.post(f'/api/sessions/{sid}/chat', json={'text':json.dumps(request), 'mode':'agent'})
                        self.assertEqual(r.status_code, 200, r.text)
                        events = [json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith('data:')]
                        result = next(e for e in events if e.get('type') == 'tool_result' and e.get('name') == 'write_file')
                        self.assertTrue(result['ok'], result)
                        self.assertEqual(result['checkpoint_status'], 'failed')
                        self.assertIn('Undo may not restore', result['output'])
                        self.assertTrue(any(e.get('type') == 'done' for e in events))
                        r = await client.get(f'/api/sessions/{sid}')
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertIn('Undo may not restore', r.text)
                finally:
                    if server.returncode is None:
                        server.send_signal(signal.SIGTERM)
                        try:
                            await asyncio.wait_for(server.wait(), 10)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()


if __name__ == '__main__':
    unittest.main()
