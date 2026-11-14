"""Opt-in fixture inheritance: real HTTP/auth/Agent; fake provider, not product gate."""
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
class InheritanceHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_parent_approval_inherited_only_during_shared_child_and_revoked(self):
        with tempfile.TemporaryDirectory(prefix='forge-inherit-http-') as td:
            root=Path(td); env=os.environ.copy()
            for k in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                      'FORGE_TEST_DEMO','FORGE_TEST_ALLOW_WRITE','FORGE_TEST_GLOBAL_LEGACY',
                      'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(k,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_ALLOW_SPAWN='1',FORGE_TEST_HOOK_INHERIT='1',PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
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
                        marker=ws/'marker'; script=f"from pathlib import Path; Path({str(marker)!r}).open('a').write('x')"
                        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':cmd}}))
                        async def chat(session,text):
                            r=await member.post(f'/api/sessions/{session}/chat',json={'text':text,'mode':'agent'}); self.assertEqual(r.status_code,200,r.text)
                            return [json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith('data:')]
                        child_task=json.dumps({'name':'check_background','args':{}})
                        prompt=json.dumps({'name':'spawn_subagent','args':{'task':child_task,'isolation':'shared'}})
                        denied=await chat(sid,prompt)
                        self.assertTrue(any(e.get('code')=='hook_trust_denied' for e in denied)); self.assertFalse(marker.exists())
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        self.assertFalse(preview['enforcement_active'])
                        self.assertEqual(preview['enforcement_scope'],'normal_non_demo_chat_and_manual_compact')
                        req={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=req); self.assertEqual(sent.status_code,200,sent.text); rid=sent.json()['request_id']
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        done=await admin.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision); self.assertEqual(done.status_code,200,done.text); self.assertFalse(done.json()['enforcement_active'])
                        events=await chat(sid,prompt)
                        self.assertFalse(any(e.get('code')=='hook_trust_denied' for e in events),events)
                        child=next(e['session_id'] for e in events if e.get('type')=='subagent_started')
                        self.assertTrue(any(e.get('type')=='subagent_done' for e in events),events)
                        self.assertEqual(marker.read_text(),'xx')
                        direct=await chat(child,child_task)
                        self.assertTrue(any(e.get('code')=='hook_trust_denied' for e in direct),direct)
                        self.assertEqual(marker.read_text(),'xx')
                        revoke=await member.delete(f'/api/workspace/hook-review-requests/{rid}'); self.assertEqual(revoke.status_code,200,revoke.text)
                        after=await chat(sid,prompt)
                        self.assertTrue(any(e.get('code')=='hook_trust_denied' for e in after),after)
                        self.assertFalse(any(e.get('type')=='subagent_started' for e in after)); self.assertEqual(marker.read_text(),'xx')

                        # Approve new exact bytes. The second invocation is the
                        # shared child's hook and waits until revocation happens.
                        release=ws/'release'; started=ws/'child-hook-started'
                        script2=(f"from pathlib import Path\nimport time\n"
                                 f"p=Path({str(marker)!r})\n"
                                 f"p.open('a').write('z')\n"
                                 f"if p.read_text().count('z') == 2:\n"
                                 f" Path({str(started)!r}).write_text('started')\n"
                                 f" while not Path({str(release)!r}).exists(): time.sleep(0.02)\n")
                        cmd2=f'{shlex.quote(sys.executable)} -c {shlex.quote(script2)}'
                        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':cmd2}}))
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        req={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        sent=await member.post('/api/workspace/hook-review-requests',json=req); self.assertEqual(sent.status_code,200,sent.text); rid=sent.json()['request_id']
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        done=await admin.post(f'/api/workspace/hook-review-requests/{rid}/decision',json=decision); self.assertEqual(done.status_code,200,done.text)
                        calls_before=(root/'fake-provider-calls').read_text().count('called')
                        running=asyncio.create_task(chat(sid,prompt))
                        async def wait_for_child_hook():
                            while not started.exists():
                                if running.done(): raise AssertionError(f'chat ended before child hook: {await running}')
                                await asyncio.sleep(.02)
                        try:
                            await asyncio.wait_for(wait_for_child_hook(),10)
                            self.assertEqual(marker.read_text(),'xxzz')
                            revoked=await admin.delete(f'/api/workspace/hook-review-requests/{rid}')
                            self.assertEqual(revoked.status_code,200,revoked.text)
                        finally:
                            release.write_text('release')
                        revoked_events=await asyncio.wait_for(running,15)
                        self.assertTrue(any(e.get('type')=='subagent_started' for e in revoked_events),revoked_events)
                        self.assertTrue(any(e.get('code')=='hook_trust_denied' for e in revoked_events),revoked_events)
                        self.assertFalse(any(e.get('type')=='subagent_done' for e in revoked_events),revoked_events)
                        self.assertFalse(any(e.get('type')=='turn_end' for e in revoked_events),revoked_events)
                        self.assertEqual(marker.read_text(),'xxzz')  # effects already happened
                        # Parent called the fake provider to spawn; child never reached it.
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),calls_before+1)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
