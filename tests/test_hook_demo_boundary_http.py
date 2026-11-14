"""Characterize demo mode with real HTTP and test-only checked hooks, NOT an enforcement test."""
import asyncio
import json
import os
import shlex
import signal
import sys
import tempfile
import unittest
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parent.parent
HARNESS=ROOT/'tests'/'fixtures'/'http_background_harness.py'
PASSWORD='fixture-password-at-least-16-characters'


@unittest.skipUnless(os.name=='posix','fixture marker commands use POSIX shell quoting')
class DemoBoundaryHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_demo_skips_checked_hooks_and_writes_even_when_review_pending(self):
        with tempfile.TemporaryDirectory(prefix='forge-demo-hook-') as td:
            root=Path(td); env=os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                        'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(key,None)
            script="from pathlib import Path; Path('global-legacy-ran').write_text('global')"
            global_hook=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
            env.update(FORGE_DATA_DIR=str(root/'data'), FORGE_TEST_ADD_MEMBER='1', FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_DEMO='1',FORGE_TEST_GLOBAL_LEGACY=global_hook,
                       PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            with (root/'server.log').open('w+') as log:
                proc=await asyncio.create_subprocess_exec(sys.executable,'-u',str(HARNESS),str(root),cwd=str(ROOT),env=env,stdout=log,stderr=log)
                try:
                    async def ready():
                        while True:
                            if proc.returncode is not None: raise AssertionError((root/'server.log').read_text())
                            try:
                                url='http://127.0.0.1:'+(root/'port').read_text()
                                async with httpx.AsyncClient(trust_env=False,timeout=.5) as c:
                                    if (await c.get(url+'/api/health')).status_code==200: return url
                            except (OSError,httpx.HTTPError): pass
                            await asyncio.sleep(.03)
                    base=await asyncio.wait_for(ready(),15)
                    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as member, \
                               httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as other:
                        async def login(c,email):
                            c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                            r=await c.post('/api/auth/login',json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code,200,r.text); c.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(other,'member2@example.test')
                        created=await member.post('/api/sessions',json={}); self.assertEqual(created.status_code,200,created.text)
                        sid=created.json()['session']['id']; ws=root/'data'/'workspaces'/sid
                        ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir(exist_ok=True)
                        marker=ws/'workspace-hook-ran'
                        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(f"from pathlib import Path; Path({str(marker)!r}).write_text(\'workspace\')")}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':cmd,'pre_tool_use':cmd,'stop':cmd}}))
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        self.assertEqual(preview['state'],'hooks'); self.assertFalse(preview['enforcement_active'])
                        body={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=body)
                        self.assertEqual(sent.status_code,200,sent.text)
                        self.assertEqual((await other.post(f'/api/sessions/{sid}/chat',json={'text':'hello'})).status_code,404)
                        memory=await member.post(f'/api/sessions/{sid}/chat',json={'text':'# demo memory marker','mode':'agent'})
                        memory_events=[json.loads(line[5:].strip()) for line in memory.text.splitlines() if line.startswith('data:')]
                        self.assertIn('demo memory marker',(ws/'MEMORY.md').read_text())
                        # HTTP intercepts quick-memory as a synthetic response before send_demo.
                        self.assertTrue(any(e.get('type')=='done' for e in memory_events),memory_events)
                        self.assertFalse(any(e.get('code')=='hook_trust_denied' for e in memory_events),memory_events)
                        self.assertFalse(marker.exists()); self.assertFalse((ws/'global-legacy-ran').exists())
                        response=await member.post(f'/api/sessions/{sid}/chat',json={'text':'hello','mode':'agent'})
                        self.assertEqual(response.status_code,200,response.text)
                        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                        self.assertTrue((ws/'hello_forge.py').is_file())
                        self.assertTrue((ws/'global-legacy-ran').exists())
                        self.assertFalse(marker.exists())
                        self.assertTrue(any(e.get('type')=='tool_result' and e.get('name')=='write_file' and e.get('ok') for e in events),events)
                        self.assertTrue(any(e.get('type')=='tool_result' and e.get('name')=='run_command' for e in events),events)
                        self.assertFalse(any(e.get('code')=='hook_trust_denied' for e in events),events)
                        self.assertTrue(any(e.get('type')=='turn_end' for e in events),events)
                        listed=(await member.get('/api/workspace/hook-review-requests')).json()
                        self.assertEqual(listed['requests'][0]['status'],'pending'); self.assertFalse(listed['enforcement_active'])
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
