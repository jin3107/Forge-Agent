"""Read-only hook review via authenticated Uvicorn; no approval or enforcement."""
import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests/fixtures/http_background_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'


class WorkspaceHookReviewHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_review_is_scoped_read_only_and_reports_override(self):
        with tempfile.TemporaryDirectory(prefix='forge-hook-review-') as td:
            root = Path(td)
            env = os.environ.copy()
            for key in ('FORGE_PROFILE', 'FORGE_RELEASE_MODE', 'FORGE_COOKIE_SECURE', 'FORGE_PUBLIC_URL', 'FORGE_TEST_BACKGROUND_GROUP_FAILURE', 'FORGE_TEST_BACKGROUND_READ_FAILURE'):
                env.pop(key, None)
            env.update(FORGE_DATA_DIR=str(root/'data'), FORGE_ALLOW_OPEN='1', PYTHONPATH=str(ROOT) + os.pathsep + env.get('PYTHONPATH', ''))
            with (root/'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(sys.executable, '-u', str(HARNESS), str(root), cwd=str(ROOT), env=env, stdout=log, stderr=log)
                try:
                    async def ready():
                        while True:
                            if server.returncode is not None:
                                raise AssertionError((root/'server.log').read_text())
                            if (root/'port').exists():
                                url = f'http://127.0.0.1:{(root/"port").read_text()}'
                                try:
                                    async with httpx.AsyncClient(trust_env=False, timeout=.5) as client:
                                        if (await client.get(url + '/api/health')).status_code == 200:
                                            return url
                                except httpx.HTTPError:
                                    pass
                            await asyncio.sleep(.03)
                    base = await asyncio.wait_for(ready(), 15)
                    async with httpx.AsyncClient(base_url=base, trust_env=False) as alice, httpx.AsyncClient(base_url=base, trust_env=False) as bob, httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                        async def login(client, email):
                            r = await client.get('/api/auth/me')
                            client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                            r = await client.post('/api/auth/login', json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code, 200, r.text)
                            client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                        await login(alice, 'alice@example.test')
                        await login(bob, 'bob@example.test')
                        r = await alice.post('/api/sessions', json={})
                        self.assertEqual(r.status_code, 200, r.text)
                        sid = r.json()['session']['id']
                        ws = root/'data'/'workspaces'/sid
                        ws.mkdir(parents=True, exist_ok=True)
                        d = ws/'.forge'
                        d.mkdir(exist_ok=True)
                        marker = ws/'must-not-run'
                        cfg = {'hooks':{'pre_tool_use':[{'command':f'touch {marker}', 'matcher':'write_file'}], 'stop':[]},
                               'on_file_change':'echo legacy', 'unrelated_private':'do-not-return'}
                        raw = json.dumps(cfg).encode()
                        (d/'config.json').write_bytes(raw)
                        route = '/api/workspace/hook-review'
                        response = await alice.get(route, params={'sid':sid})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.headers.get('cache-control'), 'no-store')
                        data = response.json()
                        self.assertEqual(data['state'], 'hooks')
                        self.assertEqual(data['workspace'], str(ws.resolve()))
                        self.assertEqual(data['digest'], hashlib.sha256(raw).hexdigest())
                        self.assertEqual(data['hooks'], cfg['hooks'])
                        self.assertEqual(data['on_file_change'], 'echo legacy')
                        self.assertTrue(data['overrides_global_hooks'])
                        self.assertFalse(data['enforcement_active'])
                        self.assertNotIn('do-not-return', response.text)
                        self.assertFalse(marker.exists())
                        empty_override = {'hooks':{'unknown':[]}}
                        (d/'config.json').write_text(json.dumps(empty_override))
                        r = await alice.get(route, params={'sid':sid})
                        self.assertEqual(r.json()['state'], 'hooks')
                        self.assertTrue(r.json()['overrides_global_hooks'])
                        self.assertNotEqual(r.json()['digest'], data['digest'])
                        literal = '<script>alert("x")</script>'
                        (d/'config.json').write_text(json.dumps({'hooks':{'stop':literal}}))
                        r = await alice.get(route, params={'sid':sid})
                        self.assertEqual(r.json()['hooks']['stop'], literal)
                        self.assertEqual((await bob.get(route, params={'sid':sid})).status_code, 404)
                        self.assertEqual((await anon.get(route, params={'sid':sid})).status_code, 401)
                        (d/'config.json').write_bytes(b'{')
                        invalid = await alice.get(route, params={'sid':sid})
                        self.assertEqual(invalid.json()['state'], 'invalid')
                        self.assertIsNone(invalid.json()['digest'])
                        self.assertIsNone(invalid.json()['hooks'])
                        self.assertFalse(marker.exists())
                        (d/'config.json').unlink()
                        missing = await alice.get(route, params={'sid':sid})
                        self.assertEqual(missing.json()['state'], 'absent')
                        self.assertFalse(missing.json()['enforcement_active'])
                        other = root/'other-workspace'
                        other.mkdir()
                        (other/'.forge').mkdir()
                        (other/'.forge'/'config.json').write_bytes(raw)
                        opened = await alice.post('/api/workspace/open', params={'sid':sid}, json={'path':str(other)})
                        self.assertEqual(opened.status_code, 200, opened.text)
                        switched = await alice.get(route, params={'sid':sid})
                        self.assertEqual(switched.json()['digest'], data['digest'])
                        self.assertEqual(switched.json()['workspace'], str(other.resolve()))
                        self.assertNotEqual(switched.json()['workspace'], data['workspace'])
                        self.assertEqual((await bob.get(route, params={'sid':sid})).status_code, 404)
                finally:
                    if server.returncode is None:
                        server.send_signal(signal.SIGTERM)
                        try:
                            await asyncio.wait_for(server.wait(), 10)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()
