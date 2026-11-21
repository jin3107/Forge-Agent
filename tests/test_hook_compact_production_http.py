"""Manual compact checks hook trust via real authenticated server, fake provider."""
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
class CompactHookHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_pending_approved_revoked_and_other_owner(self):
        with tempfile.TemporaryDirectory(prefix='forge-compact-trust-') as td:
            root=Path(td); env=os.environ.copy()
            for k in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                      'FORGE_TEST_DEMO','FORGE_TEST_GLOBAL_LEGACY','FORGE_TEST_ALLOW_WRITE',
                      'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE',
                      'FORGE_TEST_INJECTED_HOOK_RUNNER'):
                env.pop(k,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_PRODUCTION_RUNNER='1',FORGE_TEST_ALLOW_COMPACT='1',
                       PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
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
                    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as member, httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as admin, httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as other:
                        async def login(c,email):
                            c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                            r=await c.post('/api/auth/login',json={'email':email,'password':PASSWORD}); self.assertEqual(r.status_code,200,r.text)
                            c.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(admin,'alice@example.test'); await login(other,'member2@example.test')
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        prompt=json.dumps({'name':'check_background','args':{}})
                        chat=await member.post(f'/api/sessions/{sid}/chat',json={'text':prompt,'mode':'agent'}); self.assertEqual(chat.status_code,200,chat.text)
                        before=(await member.get(f'/api/sessions/{sid}')).json()['session']['messages']
                        self.assertGreater(len(before),0)
                        ws=root/'data'/'workspaces'/sid; (ws/'.forge').mkdir(parents=True,exist_ok=True)
                        marker=ws/'pre-compact'; script=f"from pathlib import Path; Path({str(marker)!r}).open('a').write('x')"
                        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'pre_compact':cmd}}))
                        url=f'/api/sessions/{sid}/compact'; calls_before=(root/'fake-provider-calls').read_text().count('called')
                        cross=await other.post(url,json={}); self.assertEqual(cross.status_code,404,cross.text)
                        blocked=await member.post(url,json={}); self.assertEqual(blocked.status_code,200,blocked.text)
                        self.assertEqual(blocked.json().get('code'),'hook_trust_denied'); self.assertFalse(marker.exists())
                        self.assertEqual((await member.get(f'/api/sessions/{sid}')).json()['session']['messages'],before)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before)
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        req={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=req); self.assertEqual(sent.status_code,200,sent.text); rid=sent.json()['request_id']
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        self.assertEqual((await member.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision)).status_code,403)
                        ok=await admin.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision); self.assertEqual(ok.status_code,200,ok.text)
                        compact=await member.post(url,json={}); self.assertEqual(compact.status_code,200,compact.text); self.assertTrue(compact.json()['ok'],compact.text)
                        self.assertEqual(marker.read_text(),'x')
                        after=(await member.get(f'/api/sessions/{sid}')).json()['session']['messages']; self.assertNotEqual(before,after)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before+1)
                        self.assertEqual((await admin.delete(f'/api/workspace/hook-review-requests/{rid}')).status_code,200)
                        denied=await member.post(url,json={}); self.assertEqual(denied.json().get('code'),'hook_trust_denied')
                        self.assertEqual(marker.read_text(),'x'); self.assertEqual((await member.get(f'/api/sessions/{sid}')).json()['session']['messages'],after)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before+1)

                        started=ws/'hook-started'; release=ws/'release'
                        script2=(f'from pathlib import Path\nimport time\n'
                                 f'Path({str(started)!r}).write_text("ran")\n'
                                 f'while not Path({str(release)!r}).exists(): time.sleep(0.02)\n')
                        cmd2=f'{shlex.quote(sys.executable)} -c {shlex.quote(script2)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'pre_compact':cmd2}}))
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        req={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=req); self.assertEqual(sent.status_code,200,sent.text); rid=sent.json()['request_id']
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        ok=await admin.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision); self.assertEqual(ok.status_code,200,ok.text)
                        pending=asyncio.create_task(member.post(url,json={}))
                        async def wait_started():
                            while not started.exists():
                                if pending.done(): raise AssertionError(f'compact ended early: {(await pending).text}')
                                await asyncio.sleep(.02)
                        try:
                            await asyncio.wait_for(wait_started(),10)
                            self.assertEqual((await admin.delete(f'/api/workspace/hook-review-requests/{rid}')).status_code,200)
                        finally:
                            release.write_text('release')
                        blocked_after=await asyncio.wait_for(pending,15)
                        self.assertEqual(blocked_after.json().get('code'),'hook_trust_denied')
                        self.assertEqual(started.read_text(),'ran')  # prior side effect remains
                        self.assertEqual((await member.get(f'/api/sessions/{sid}')).json()['session']['messages'],after)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before+1)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
