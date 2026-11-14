"""Real HTTP integration of *test-only* prototype wiring, not product enforcement."""
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
class CheckedAdapterHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_member_admin_review_and_checked_agent_paths(self):
        with tempfile.TemporaryDirectory(prefix='forge-checked-http-') as td:
            root=Path(td); env=os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                        'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(key,None)
            env.update(FORGE_DATA_DIR=str(root/'data'), FORGE_TEST_ADD_MEMBER='1', FORGE_TEST_HOOK_ADAPTER='1',
                       PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            with (root/'server.log').open('w+') as log:
                proc=await asyncio.create_subprocess_exec(sys.executable,'-u',str(HARNESS),str(root),cwd=str(ROOT),env=env,stdout=log,stderr=log)
                try:
                    async def ready():
                        while True:
                            if proc.returncode is not None: raise AssertionError((root/'server.log').read_text())
                            try:
                                base='http://127.0.0.1:'+(root/'port').read_text()
                                async with httpx.AsyncClient(trust_env=False,timeout=.5) as c:
                                    if (await c.get(base+'/api/health')).status_code==200: return base
                            except (OSError,httpx.HTTPError): pass
                            await asyncio.sleep(.03)
                    base=await asyncio.wait_for(ready(),15)
                    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as member, \
                               httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as admin, \
                               httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as other:
                        async def login(c,email):
                            c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                            r=await c.post('/api/auth/login',json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code,200,r.text)
                            c.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(admin,'alice@example.test'); await login(other,'member2@example.test')
                        created=await member.post('/api/sessions',json={}); self.assertEqual(created.status_code,200,created.text)
                        sid=created.json()['session']['id']; ws=root/'data'/'workspaces'/sid
                        ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir(exist_ok=True)
                        def command(label):
                            script=f"from pathlib import Path; p=Path({str(ws/label)!r}); p.open('a').write('x'); print('context')"
                            return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        config=ws/'.forge'/'config.json'
                        original=json.dumps({'hooks':{'user_prompt_submit':command('prompt'),
                             'session_start':command('start'), 'pre_tool_use':command('pre'),
                             'post_tool_use':command('post'), 'stop':command('stop')}})
                        config.write_text(original)
                        text=json.dumps({'name':'check_background','args':{}})
                        async def chat():
                            r=await member.post(f'/api/sessions/{sid}/chat',json={'text':text,'mode':'agent'})
                            self.assertEqual(r.status_code,200,r.text)
                            return [json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith('data:')]
                        def denied(events):
                            self.assertTrue(any(e.get('type')=='error' and e.get('code')=='hook_trust_denied' for e in events),events)
                            self.assertFalse(any(e.get('type')=='turn_end' for e in events),events)
                        def counts():
                            return {n:len((ws/n).read_text()) if (ws/n).exists() else 0 for n in ('prompt','start','pre','post','stop')}
                        denied(await chat())
                        self.assertEqual(counts(),dict.fromkeys(('prompt','start','pre','post','stop'),0))
                        self.assertFalse((root/'fake-provider-calls').exists())
                        saved=(await member.get(f'/api/sessions/{sid}')).json()['session']['messages']
                        self.assertEqual(saved[-1]['content'],text)
                        self.assertEqual((await other.post(f'/api/sessions/{sid}/chat',json={'text':text})).status_code,404)
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        request={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'],'share_commands':True}
                        url='/api/workspace/hook-review-requests'
                        sent=await member.post(url,json=request); self.assertEqual(sent.status_code,200,sent.text)
                        rid=sent.json()['request_id']; self.assertFalse(sent.json()['enforcement_active'])
                        denied(await chat()); self.assertFalse((root/'fake-provider-calls').exists())
                        decision={'decision':'approve','expected_workspace':preview['workspace'],'expected_digest':preview['digest']}
                        self.assertEqual((await member.post(f'{url}/{rid}/decision',json=decision)).status_code,403)
                        approved=await admin.post(f'{url}/{rid}/decision',json=decision)
                        self.assertEqual(approved.status_code,200,approved.text)
                        self.assertFalse(approved.json()['enforcement_active'])
                        for _ in range(2):
                            events=await chat()
                            self.assertFalse(any(e.get('type')=='error' for e in events),events)
                            self.assertTrue(any(e.get('type')=='turn_end' for e in events),events)
                            self.assertTrue(any(e.get('type')=='tool_result' and e.get('name')=='check_background' for e in events),events)
                        self.assertEqual(counts(),{'prompt':2,'start':1,'pre':2,'post':2,'stop':2})
                        self.assertEqual((await member.delete(f'{url}/{rid}')).status_code,200)
                        provider_before=(root/'fake-provider-calls').read_text()
                        denied(await chat())
                        self.assertEqual((root/'fake-provider-calls').read_text(),provider_before)
                        sent=await member.post(url,json=request); rid2=sent.json()['request_id']; self.assertNotEqual(rid,rid2)
                        self.assertEqual((await admin.post(f'{url}/{rid2}/decision',json=decision)).status_code,200)
                        self.assertTrue(any(e.get('type')=='turn_end' for e in await chat()))
                        self.assertEqual(counts()['start'],2)
                        config.write_text(json.dumps({'hooks':{'user_prompt_submit':command('unreviewed')}}))
                        provider_before=(root/'fake-provider-calls').read_text()
                        denied(await chat()); self.assertFalse((ws/'unreviewed').exists())
                        config.write_text(original)
                        denied(await chat())
                        self.assertEqual((root/'fake-provider-calls').read_text(),provider_before)
                        # A real in-flight hook finishes after the owner revokes;
                        # the next hook and tool must not run in the test adapter.
                        sid2=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws2=root/'data'/'workspaces'/sid2; ws2.mkdir(parents=True,exist_ok=True); (ws2/'.forge').mkdir(exist_ok=True)
                        first=ws2/'first'; release=ws2/'release'; second=ws2/'second'
                        script=(f"from pathlib import Path; import time; Path({str(first)!r}).write_text('started'); "
                                f"p=Path({str(release)!r}); deadline=time.monotonic()+8; "
                                "exec('while not p.exists() and time.monotonic() < deadline:\\n time.sleep(0.01)')")
                        wait_command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        second_script=f"from pathlib import Path; Path({str(second)!r}).write_text('ran')"
                        second_command=f'{shlex.quote(sys.executable)} -c {shlex.quote(second_script)}'
                        (ws2/'.forge'/'config.json').write_text(json.dumps({'hooks':{'pre_tool_use':[wait_command,second_command]}}))
                        preview2=(await member.get('/api/workspace/hook-review',params={'sid':sid2})).json()
                        request2={'sid':sid2,'expected_workspace':preview2['workspace'],'expected_digest':preview2['digest'],'share_commands':True}
                        rid3=(await member.post(url,json=request2)).json()['request_id']
                        decision2={'decision':'approve','expected_workspace':preview2['workspace'],'expected_digest':preview2['digest']}
                        self.assertEqual((await admin.post(f'{url}/{rid3}/decision',json=decision2)).status_code,200)
                        running=asyncio.create_task(member.post(f'/api/sessions/{sid2}/chat',json={'text':text,'mode':'agent'}))
                        try:
                            async def wait_first():
                                while not first.exists(): await asyncio.sleep(.02)
                            await asyncio.wait_for(wait_first(),5)
                            self.assertEqual((await member.delete(f'{url}/{rid3}')).status_code,200)
                            release.write_text('go')
                            response=await asyncio.wait_for(running,10)
                        finally:
                            release.write_text('go')
                            if not running.done():
                                running.cancel(); await asyncio.gather(running,return_exceptions=True)
                        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                        denied(events)
                        self.assertTrue(first.exists()); self.assertFalse(second.exists())
                        self.assertFalse(any(e.get('type')=='tool_result' and e.get('name')=='check_background' for e in events),events)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM):
                        print((root/'server.log').read_text())
