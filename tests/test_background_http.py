"""Two-account background scoping over real loopback HTTP with a fake LLM."""
import asyncio
import json
import os
import re
import shlex
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests' / 'fixtures' / 'http_background_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'
MARKER = 'PRIVATE_HTTP_BACKGROUND_MARKER'


@unittest.skipUnless(os.name == 'posix', 'fixture cleanup uses POSIX process groups')
class BackgroundHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_two_accounts_and_sessions_cannot_access_other_background_tasks(self):
        await self._exercise(False)

    async def test_unconfirmed_direct_stop_is_not_ok_over_http(self):
        await self._exercise(True)

    async def _exercise(self, inject_group_failure):
        with tempfile.TemporaryDirectory(prefix='forge-http-bg-') as td:
            root = Path(td)
            ws = root / 'shared-workspace'
            ws.mkdir()
            worker = ws / 'worker.py'
            worker.write_text("import os,pathlib,time\n"
                              "tmp=pathlib.Path('worker.pid.part'); tmp.write_text(str(os.getpid())); os.replace(tmp,'worker.pid')\n"
                              f"print('{MARKER}', flush=True)\ntime.sleep(60)\n")
            env = os.environ.copy()
            for key in ('OPENROUTER_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'FORGE_CUSTOM_API_KEY',
                        'FORGE_PROVIDER', 'FORGE_MODEL', 'FORGE_RELEASE_MODE', 'FORGE_PUBLIC_URL',
                        'FORGE_COOKIE_SECURE', 'FORGE_PROFILE'):
                env.pop(key, None)
            env.update(FORGE_DATA_DIR=str(root / 'data'), FORGE_ALLOW_OPEN='1',
                       FORGE_TEST_BACKGROUND_READ_FAILURE='1',
                       PYTHONPATH=str(ROOT) + os.pathsep + env.get('PYTHONPATH', ''))
            if inject_group_failure:
                env['FORGE_TEST_BACKGROUND_GROUP_FAILURE'] = '1'
            else:
                env.pop('FORGE_TEST_BACKGROUND_GROUP_FAILURE', None)
            with (root / 'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(
                    sys.executable, '-u', str(HARNESS), str(root), cwd=str(ROOT), env=env,
                    stdout=log, stderr=log)
                try:
                    async def ready():
                        while True:
                            if server.returncode is not None:
                                raise AssertionError(f'server exited: {(root / "server.log").read_text()}')
                            if (root / 'port').exists():
                                url = f'http://127.0.0.1:{(root / "port").read_text()}'
                                try:
                                    async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                                        response = await client.get(url + '/api/health')
                                        if response.status_code == 200:
                                            return url
                                except httpx.HTTPError:
                                    pass
                            await asyncio.sleep(0.03)
                    base = await asyncio.wait_for(ready(), 15)
                    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as alice, \
                               httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as bob, \
                               httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as anonymous:
                        async def login(client, email):
                            response = await client.get('/api/auth/me')
                            self.assertEqual(response.status_code, 200, response.text)
                            client.headers['X-CSRF-Token'] = response.json()['csrf_token']
                            response = await client.post('/api/auth/login', json={'email': email, 'password': PASSWORD})
                            self.assertEqual(response.status_code, 200, response.text)
                            client.headers['X-CSRF-Token'] = response.json()['csrf_token']
                        async def session(client):
                            response = await client.post('/api/sessions', json={})
                            self.assertEqual(response.status_code, 200, response.text)
                            sid = response.json()['session']['id']
                            response = await client.post('/api/workspace/open', params={'sid': sid}, json={'path': str(ws)})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(Path(response.json()['workspace']).resolve(), ws.resolve())
                            return sid
                        async def chat(client, sid, name, args):
                            text = json.dumps({'name': name, 'args': args})
                            response = await client.post(f'/api/sessions/{sid}/chat', json={'text': text, 'mode': 'agent'})
                            self.assertEqual(response.status_code, 200, response.text)
                            events = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                                      if line.startswith('data:')]
                            self.assertTrue(any(e.get('type') == 'done' for e in events), events)
                            self.assertFalse(any(e.get('type') == 'error' for e in events), events)
                            results = [e for e in events if e.get('type') == 'tool_result' and e.get('name') == name]
                            self.assertEqual(len(results), 1, events)
                            return results[0]
                        await login(alice, 'alice@example.test')
                        await login(bob, 'bob@example.test')
                        self.assertEqual((await anonymous.get('/api/sessions')).status_code, 401)
                        a1, a2, b1 = await session(alice), await session(alice), await session(bob)
                        self.assertEqual((await bob.get(f'/api/sessions/{a1}')).status_code, 404)
                        cross = await bob.post(f'/api/sessions/{a1}/chat', json={'text': 'x'})
                        self.assertEqual(cross.status_code, 404, cross.text)
                        bad_csrf = await bob.post('/api/sessions', headers={'X-CSRF-Token': 'wrong'}, json={})
                        self.assertEqual(bad_csrf.status_code, 403, bad_csrf.text)
                        command = 'exec ' + shlex.quote(sys.executable) + ' -u ' + shlex.quote(str(worker))
                        started = await chat(alice, a1, 'run_background', {'command': command})
                        self.assertTrue(started['ok'], started)
                        self.assertIn(MARKER, started['output'])
                        self.assertIn('output may be incomplete', started['output'].lower())
                        self.assertNotIn('PRIVATE_INJECTED_READ_ERROR', started['output'])
                        match = re.search(r'\[(bg-[0-9a-f]{32})\]', started['output'])
                        self.assertIsNotNone(match, started)
                        tid = match.group(1)
                        for client, sid in ((bob, b1), (alice, a2)):
                            listed = await chat(client, sid, 'check_background', {})
                            self.assertTrue(listed['ok'], listed)
                            self.assertNotIn(tid, listed['output'])
                            self.assertNotIn(MARKER, listed['output'])
                            for name in ('check_background', 'kill_background'):
                                denied = await chat(client, sid, name, {'id': tid})
                                self.assertFalse(denied['ok'], denied)
                                self.assertNotIn(MARKER, denied['output'])
                        own = await chat(alice, a1, 'check_background', {})
                        self.assertIn(tid, own['output'])
                        self.assertIn('⏳', own['output'])
                        self.assertIn('output may be incomplete', own['output'].lower())
                        checked = await chat(alice, a1, 'check_background', {'id':tid})
                        self.assertIn('output may be incomplete', checked['output'].lower())
                        self.assertNotIn('PRIVATE_INJECTED_READ_ERROR', checked['output'])
                        killed = await chat(alice, a1, 'kill_background', {'id': tid})
                        self.assertEqual(killed['ok'], not inject_group_failure, killed)
                        self.assertIn('kill', killed['output'].lower())
                        self.assertIn('output may be incomplete', killed['output'].lower())
                        if inject_group_failure:
                            self.assertIn('group stop could not be confirmed', killed['output'])
                            repeated = await chat(alice, a1, 'kill_background', {'id': tid})
                            self.assertFalse(repeated['ok'], repeated)
                            self.assertIn('group stop could not be confirmed', repeated['output'])
                finally:
                    if server.returncode is None:
                        server.send_signal(signal.SIGTERM)
                        try:
                            await asyncio.wait_for(server.wait(), 10)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()
                    pidfile = ws / 'worker.pid'
                    if pidfile.exists():
                        value = pidfile.read_text()
                        self.assertTrue(value.isascii() and value.isdecimal(), f'invalid fixture PID: {value!r}')
                        pid = int(value)
                        self.assertGreater(pid, 1)
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
