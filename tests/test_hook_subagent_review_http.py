"""Session-scoped review with test-only checked hook adapter, real HTTP and subagent."""
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


@unittest.skipUnless(os.name=='posix','marker commands use POSIX shell quoting')
class SubagentReviewHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_parent_approval_does_not_authorize_new_shared_workspace_children(self):
        with tempfile.TemporaryDirectory(prefix='forge-sub-review-') as td:
            root=Path(td); env=os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                        'FORGE_TEST_DEMO','FORGE_TEST_ALLOW_WRITE','FORGE_TEST_GLOBAL_LEGACY',
                        'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(key,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_ALLOW_SPAWN='1',PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            with (root/'server.log').open('w+') as log:
                proc=await asyncio.create_subprocess_exec(sys.executable,'-u',str(HARNESS),str(root),cwd=ROOT,env=env,stdout=log,stderr=log)
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
                               httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as admin:
                        async def login(client,email):
                            client.headers['X-CSRF-Token']=(await client.get('/api/auth/me')).json()['csrf_token']
                            r=await client.post('/api/auth/login',json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code,200,r.text); client.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(admin,'alice@example.test')
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir()
                        marker=ws/'prompt-marker'
                        script=f"from pathlib import Path; p=Path({str(marker)!r}); p.open('a').write('x')"
                        command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':command}}))
                        url='/api/workspace/hook-review-requests'
                        async def approve(session_id):
                            preview=await member.get('/api/workspace/hook-review',params={'sid':session_id})
                            self.assertEqual(preview.status_code,200,preview.text)
                            info=preview.json(); self.assertEqual(info['workspace'],str(ws.resolve()))
                            req={'sid':session_id,'expected_workspace':info['workspace'],'expected_digest':info['digest'],'share_commands':True}
                            sent=await member.post(url,json=req); self.assertEqual(sent.status_code,200,sent.text)
                            rid=sent.json()['request_id']
                            decision={'decision':'approve','expected_workspace':info['workspace'],'expected_digest':info['digest']}
                            done=await admin.post(f'{url}/{rid}/decision',json=decision)
                            self.assertEqual(done.status_code,200,done.text); self.assertFalse(done.json()['enforcement_active'])
                            return rid
                        await approve(sid)
                        prompt=json.dumps({'name':'spawn_subagent','args':{'task':'child task','isolation':'shared'}})
                        async def chat():
                            response=await member.post(f'/api/sessions/{sid}/chat',json={'text':prompt,'mode':'agent'})
                            self.assertEqual(response.status_code,200,response.text)
                            events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                            self.assertTrue(any(e.get('type')=='error' and e.get('code')=='hook_trust_denied' for e in events),events)
                            self.assertFalse(any(e.get('type')=='subagent_done' for e in events),events)
                            self.assertFalse(any(e.get('type')=='turn_end' for e in events),events)
                            children=[e['session_id'] for e in events if e.get('type')=='subagent_started']
                            self.assertEqual(len(children),1,events)
                            return children[0]
                        first=await chat()
                        self.assertEqual(marker.read_text(),'x')
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),1)
                        self.assertEqual((await member.get(f'/api/sessions/{first}')).status_code,200)
                        await approve(first)
                        second=await chat()
                        self.assertNotEqual(first,second)
                        self.assertEqual(marker.read_text(),'xx')
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),2)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
