"""Opt-in checked adapter: real HTTP and background child, fake provider."""
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

@unittest.skipUnless(os.name=='posix','POSIX shell quoting')
class BackgroundInheritanceHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_during_background_child_session_start(self):
        with tempfile.TemporaryDirectory(prefix='forge-bg-inherit-') as td:
            root=Path(td); env=os.environ.copy()
            for k in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                      'FORGE_TEST_DEMO','FORGE_TEST_GLOBAL_LEGACY','FORGE_TEST_ALLOW_WRITE',
                      'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(k,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_ALLOW_BACKGROUND_AGENT='1',FORGE_TEST_HOOK_INHERIT='1',PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
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
                    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as member, httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as admin:
                        async def login(client,email):
                            client.headers['X-CSRF-Token']=(await client.get('/api/auth/me')).json()['csrf_token']
                            r=await client.post('/api/auth/login',json={'email':email,'password':PASSWORD}); self.assertEqual(r.status_code,200,r.text)
                            client.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(admin,'alice@example.test')
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir()
                        started=ws/'bg-hook-started'; release=ws/'release'
                        script=(f"import json,sys,time\nfrom pathlib import Path\n"
                                f"sid=json.load(sys.stdin)['session_id']\n"
                                f"if sid != {sid!r}:\n"
                                f" Path({str(started)!r}).write_text(sid)\n"
                                f" while not Path({str(release)!r}).exists(): time.sleep(0.02)\n")
                        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'session_start':cmd}}))
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        req={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=req); self.assertEqual(sent.status_code,200,sent.text); rid=sent.json()['request_id']
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        approved=await admin.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision); self.assertEqual(approved.status_code,200,approved.text); self.assertFalse(approved.json()['enforcement_active'])
                        child_task=json.dumps({'name':'check_background','args':{}})
                        prompt=json.dumps({'name':'spawn_background_agent','args':{'task':child_task,'isolation':'shared'}})
                        # First prove a shared background child completes while
                        # approval remains valid, then repeat with revocation.
                        release.write_text('release')
                        first=await member.post(f'/api/sessions/{sid}/chat',json={'text':prompt,'mode':'agent'}); self.assertEqual(first.status_code,200,first.text)
                        first_events=[json.loads(line[5:].strip()) for line in first.text.splitlines() if line.startswith('data:')]
                        first_result=next((e for e in first_events if e.get('type')=='tool_result' and e.get('name')=='spawn_background_agent'),None)
                        self.assertIsNotNone(first_result,first_events); self.assertTrue(first_result.get('ok'),first_events)
                        async def wait_first_done():
                            while True:
                                if started.exists():
                                    child=started.read_text()
                                    r=await member.get(f'/api/sessions/{sid}/messages',params={'peek':'true'})
                                    self.assertEqual(r.status_code,200,r.text)
                                    if any(m.get('from')==child and 'XONG' in m.get('text','') for m in r.json()['messages']):
                                        return child
                                await asyncio.sleep(.05)
                        first_child=await asyncio.wait_for(wait_first_done(),10)
                        started.unlink(); release.unlink()
                        response=await member.post(f'/api/sessions/{sid}/chat',json={'text':prompt,'mode':'agent'}); self.assertEqual(response.status_code,200,response.text)
                        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                        result=next((e for e in events if e.get('type')=='tool_result' and e.get('name')=='spawn_background_agent'),None)
                        self.assertIsNotNone(result,events); self.assertTrue(result.get('ok'),events)
                        async def wait_started():
                            while not started.exists(): await asyncio.sleep(.02)
                        try:
                            await asyncio.wait_for(wait_started(),10)
                            child_sid=started.read_text(); self.assertNotEqual(child_sid,sid); self.assertNotEqual(child_sid,first_child)
                            self.assertEqual((await member.get(f'/api/sessions/{child_sid}')).status_code,200)
                            calls_before=(root/'fake-provider-calls').read_text().count('called')
                            revoked=await admin.delete(f'/api/workspace/hook-review-requests/{rid}')
                            self.assertEqual(revoked.status_code,200,revoked.text)
                        finally:
                            release.write_text('release')
                        async def wait_inbox():
                            while True:
                                r=await member.get(f'/api/sessions/{sid}/messages',params={'peek':'true'})
                                self.assertEqual(r.status_code,200,r.text)
                                messages=r.json()['messages']
                                match=[m for m in messages if m.get('from')==child_sid and 'hook_trust_denied' in m.get('text','')]
                                if match: return match
                                await asyncio.sleep(.05)
                        messages=await asyncio.wait_for(wait_inbox(),10)
                        self.assertTrue(any('LỖI' in m['text'] for m in messages),messages)
                        self.assertEqual(started.read_text(),child_sid)  # prior hook write remains
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
