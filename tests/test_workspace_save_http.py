"""Real authenticated HTTP direct-save checkpoint and undo integration."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from engine import checkpoints

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests/fixtures/http_background_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'


@unittest.skipUnless(checkpoints.available(), 'git unavailable')
class WorkspaceSaveHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_auth_save_twice_and_undo_twice(self):
        with tempfile.TemporaryDirectory(prefix='forge-save-http-') as td:
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
                                    async with httpx.AsyncClient(trust_env=False, timeout=.5) as c:
                                        if (await c.get(base + '/api/health')).status_code == 200:
                                            return base
                                except httpx.HTTPError:
                                    pass
                            await asyncio.sleep(.03)
                    base = await asyncio.wait_for(ready(), 15)
                    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as alice, \
                               httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as bob:
                        async def login(client, email):
                            r = await client.get('/api/auth/me')
                            client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                            r = await client.post('/api/auth/login', json={'email':email, 'password':PASSWORD})
                            self.assertEqual(r.status_code, 200, r.text)
                            client.headers['X-CSRF-Token'] = r.json()['csrf_token']
                        await login(alice, 'alice@example.test')
                        await login(bob, 'bob@example.test')
                        r = await alice.post('/api/sessions', json={})
                        self.assertEqual(r.status_code, 200, r.text)
                        sid = r.json()['session']['id']
                        target = root / 'data' / 'workspaces' / sid / 'versioned.txt'
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b'original')
                        async def versioned(client, content, version, headers=None):
                            return await client.put('/api/workspace/file/conditional', params={'sid':sid}, headers=headers,
                                json={'path':'versioned.txt', 'content':content, 'expected_version':version})
                        async def preview():
                            return await alice.get('/api/workspace/file', params={'sid':sid, 'path':'versioned.txt'})
                        first = (await preview()).json()['version']
                        missing_version = await alice.put('/api/workspace/file/conditional', params={'sid':sid}, json={'path':'versioned.txt', 'content':'draft'})
                        self.assertEqual(missing_version.status_code, 422, missing_version.text)
                        self.assertEqual((await versioned(alice, 'draft', None)).status_code, 422)
                        self.assertEqual((await versioned(alice, 'draft', 'invalid')).status_code, 422)
                        self.assertEqual(target.read_bytes(), b'original')
                        legacy_conditional = await alice.put('/api/workspace/file', params={'sid':sid}, json={'path':'versioned.txt', 'content':'draft', 'expected_version':'0'*64})
                        self.assertEqual(legacy_conditional.status_code, 409, legacy_conditional.text)
                        self.assertEqual(target.read_bytes(), b'original')
                        self.assertEqual((await versioned(bob, 'intrusion', first)).status_code, 404)
                        self.assertEqual((await versioned(alice, 'bad', first, {'X-CSRF-Token':'wrong'})).status_code, 403)
                        too_large = await versioned(alice, 'é' * (2 * 1024 * 1024 + 1), first)
                        self.assertEqual(too_large.status_code, 413, too_large.text)
                        self.assertEqual(target.read_bytes(), b'original')
                        nul = await versioned(alice, 'draft\0suffix', first)
                        self.assertEqual(nul.status_code, 422, nul.text)
                        self.assertEqual(target.read_bytes(), b'original')
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            anon.headers['X-CSRF-Token'] = (await anon.get('/api/auth/me')).json()['csrf_token']
                            self.assertEqual((await versioned(anon, 'bad', first)).status_code, 401)
                        target.write_bytes(b'external')
                        r = await versioned(alice, 'draft', first)
                        self.assertEqual(r.status_code, 409, r.text)
                        self.assertEqual(target.read_bytes(), b'external')
                        current = (await preview()).json()['version']
                        r = await versioned(alice, 'first save', current)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertNotEqual(r.json()['version'], current)
                        r = await versioned(alice, 'second save', r.json()['version'])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(target.read_bytes(), b'second save')
                        last = r.json()['version']
                        async def conditional_delete(client, version, headers=None):
                            return await client.delete('/api/workspace/file/conditional', params={'sid':sid, 'path':'versioned.txt', 'expected_version':version}, headers=headers)
                        self.assertEqual((await conditional_delete(bob, last)).status_code, 404)
                        self.assertEqual((await conditional_delete(alice, last, {'X-CSRF-Token':'wrong'})).status_code, 403)
                        self.assertEqual((await conditional_delete(alice, 'invalid')).status_code, 422)
                        self.assertEqual((await alice.delete('/api/workspace/file/conditional', params={'sid':sid, 'path':'versioned.txt'})).status_code, 422)
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            anon.headers['X-CSRF-Token'] = (await anon.get('/api/auth/me')).json()['csrf_token']
                            self.assertEqual((await conditional_delete(anon, last)).status_code, 401)
                        target.write_bytes(b'external before delete')
                        r = await conditional_delete(alice, last)
                        self.assertEqual(r.status_code, 409, r.text)
                        self.assertEqual(target.read_bytes(), b'external before delete')
                        fresh = (await preview()).json()['version']
                        r = await conditional_delete(alice, fresh)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertFalse(target.exists())
                        r = await versioned(alice, 'draft', last)
                        self.assertEqual(r.status_code, 409, r.text)
                        self.assertFalse(target.exists())
                        async def save(client, text, headers=None):
                            return await client.put('/api/workspace/file', params={'sid':sid},
                                json={'path':'file.txt', 'content':text}, headers=headers)
                        self.assertEqual((await save(bob, 'intrusion')).status_code, 404)
                        self.assertEqual((await save(alice, 'bad-csrf', {'X-CSRF-Token':'wrong'})).status_code, 403)
                        r = await alice.put('/api/workspace/file', params={'sid':sid}, json={'path':'../escape', 'content':'bad'})
                        self.assertEqual(r.status_code, 400, r.text)
                        for value in ('one', 'two'):
                            r = await save(alice, value)
                            self.assertEqual(r.status_code, 200, r.text)
                            self.assertEqual(r.json()['checkpoint_status'], 'created', r.text)
                            self.assertNotIn('warning', r.json())
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertTrue(r.json()['ok'], r.text)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})
                        self.assertEqual(r.json()['content'], 'one')
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertTrue(r.json()['ok'], r.text)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})
                        self.assertEqual(r.status_code, 404, r.text)
                        async def create(client, name, content='new', headers=None):
                            return await client.post('/api/workspace/file', params={'sid':sid}, headers=headers,
                                json={'path':name, 'content':content})
                        self.assertEqual((await create(bob, 'created.txt')).status_code, 404)
                        self.assertEqual((await create(alice, 'created.txt', headers={'X-CSRF-Token':'wrong'})).status_code, 403)
                        self.assertEqual((await create(alice, '../escape')).status_code, 400)
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            anon.headers['X-CSRF-Token'] = (await anon.get('/api/auth/me')).json()['csrf_token']
                            self.assertEqual((await create(anon, 'created.txt')).status_code, 401)
                        r = await create(alice, 'created.txt', 'original')
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['checkpoint_status'], 'created')
                        r = await create(alice, 'created.txt', 'overwrite')
                        self.assertEqual(r.status_code, 409, r.text)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'created.txt'})
                        self.assertEqual(r.json()['content'], 'original')
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertTrue(r.json()['ok'], r.text)
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'created.txt'})).status_code, 404)
                        r = await save(alice, 'restore after delete')
                        self.assertEqual(r.status_code, 200, r.text)
                        async def delete(client, path='file.txt', headers=None):
                            return await client.delete('/api/workspace/file', params={'sid':sid, 'path':path}, headers=headers)
                        self.assertEqual((await delete(bob)).status_code, 404)
                        self.assertEqual((await delete(alice, headers={'X-CSRF-Token':'wrong'})).status_code, 403)
                        self.assertEqual((await delete(alice, '../escape')).status_code, 400)
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            self.assertEqual((await delete(anon)).status_code, 403)
                            anon.headers['X-CSRF-Token'] = (await anon.get('/api/auth/me')).json()['csrf_token']
                            self.assertEqual((await delete(anon)).status_code, 401)
                        r = await delete(alice)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['checkpoint_status'], 'created')
                        self.assertIn('ignored', r.json()['notice'].lower())
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})).status_code, 404)
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertTrue(r.json()['ok'], r.text)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})
                        self.assertEqual(r.json()['content'], 'restore after delete')
                        async def upload(client, headers=None, overwrite=False):
                            return await client.post('/api/workspace/upload', params={'sid':sid}, headers=headers,
                                data={'overwrite':'true'} if overwrite else {},
                                files=[('files', ('file.txt', b'uploaded')), ('files', ('new.txt', b'new'))])
                        self.assertEqual((await upload(bob)).status_code, 404)
                        self.assertEqual((await upload(alice, {'X-CSRF-Token':'wrong'})).status_code, 403)
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            anon.headers['X-CSRF-Token'] = (await anon.get('/api/auth/me')).json()['csrf_token']
                            self.assertEqual((await upload(anon)).status_code, 401)
                        r = await alice.post('/api/workspace/upload', params={'sid':sid}, files=[('files', ('file.txt', b'intrusion'))])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['saved'], [])
                        self.assertEqual(r.json()['skipped'], ['file.txt (already exists)'])
                        self.assertNotIn('checkpoint_status', r.json())
                        r = await upload(alice)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['saved'], ['new.txt'])
                        self.assertEqual(r.json()['skipped'], ['file.txt (already exists)'])
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})).json()['content'], 'restore after delete')
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertTrue(r.json()['ok'], r.text)
                        r = await upload(alice, overwrite=True)
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['saved'], ['file.txt', 'new.txt'])
                        self.assertEqual(r.json()['checkpoint_status'], 'created')
                        self.assertIn('ignored', r.json()['notice'].lower())
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertTrue(r.json()['ok'], r.text)
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'file.txt'})).json()['content'], 'restore after delete')
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'new.txt'})).status_code, 404)
                        r = await alice.post('/api/workspace/upload', params={'sid':sid},
                            files=[('files', (f'batch-{i}.txt', str(i).encode())) for i in range(1,23)])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(len(r.json()['saved']), 20)
                        self.assertEqual(r.json()['skipped'], ['#21: batch-21.txt (over 20-file limit)', '#22: batch-22.txt (over 20-file limit)'])
                        self.assertEqual(r.json()['checkpoint_status'], 'created')
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'batch-20.txt'})).status_code, 200)
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'batch-21.txt'})).status_code, 404)
                        r = await alice.post(f'/api/sessions/{sid}/undo')
                        self.assertTrue(r.json()['ok'], r.text)
                        limit = 20 * 1024 * 1024
                        r = await alice.post('/api/workspace/upload', params={'sid':sid}, files=[
                            ('files', ('too-large.bin', b'x' * (limit + 1))),
                            ('files', ('at-limit.bin', b'y' * limit))])
                        self.assertEqual(r.status_code, 200, r.text)
                        self.assertEqual(r.json()['saved'], ['at-limit.bin'])
                        self.assertEqual(r.json()['skipped'], ['too-large.bin (>20MB)'])
                        self.assertEqual(r.json()['checkpoint_status'], 'created')
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'at-limit.bin'})).status_code, 413)
                        preview_limit = 4 * 1024 * 1024
                        r = await alice.post('/api/workspace/upload', params={'sid':sid}, files=[
                            ('files', ('preview-exact.txt', b'a' * preview_limit)),
                            ('files', ('preview-over.txt', b'b' * (preview_limit + 1)))])
                        self.assertEqual(r.status_code, 200, r.text)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'preview-exact.txt'})
                        self.assertEqual(r.status_code, 200, r.text[:200])
                        self.assertEqual(len(r.json()['content']), preview_limit)
                        r = await alice.get('/api/workspace/file', params={'sid':sid, 'path':'preview-over.txt'})
                        self.assertEqual(r.status_code, 413, r.text)
                        self.assertIn('4 MiB', r.json()['detail'])
                        self.assertEqual((await bob.get('/api/workspace/file', params={'sid':sid, 'path':'preview-over.txt'})).status_code, 404)
                        self.assertEqual((await alice.get('/api/workspace/file', params={'sid':sid, 'path':'../escape'})).status_code, 400)
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as anon:
                            self.assertEqual((await anon.get('/api/workspace/file', params={'sid':sid, 'path':'preview-over.txt'})).status_code, 401)
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
