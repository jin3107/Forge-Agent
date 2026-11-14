"""Characterize real HTTP write with an opt-in checked test adapter, not production enforcement."""
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


@unittest.skipUnless(os.name=='posix','fixture commands use POSIX shell quoting')
class CheckedWriteHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_config_change_during_tool_precedes_denial_and_global_hook(self):
        with tempfile.TemporaryDirectory(prefix='forge-checked-write-') as td:
            root=Path(td); env=os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                        'FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(key,None)
            legacy_script="from pathlib import Path; Path('global-hook-ran').write_text('global hook ran')"
            legacy=f'{shlex.quote(sys.executable)} -c {shlex.quote(legacy_script)}'
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',FORGE_TEST_HOOK_ADAPTER='1',
                       FORGE_TEST_ALLOW_WRITE='1',FORGE_TEST_GLOBAL_LEGACY=legacy,
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
                    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=20) as member:
                        member.headers['X-CSRF-Token']=(await member.get('/api/auth/me')).json()['csrf_token']
                        login=await member.post('/api/auth/login',json={'email':'member@example.test','password':PASSWORD})
                        self.assertEqual(login.status_code,200,login.text); member.headers['X-CSRF-Token']=login.json()['csrf_token']
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True)
                        (ws/'.forge').mkdir(exist_ok=True); config=ws/'.forge'/'config.json'; config.write_text('{}')
                        bad_marker=ws/'unapproved-hook-ran'
                        script=f"from pathlib import Path; Path({str(bad_marker)!r}).write_text('ran')"
                        unapproved=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                        new_config=json.dumps({'hooks':{'stop':unapproved}})
                        request=json.dumps({'name':'write_file','args':{'path':'.forge/config.json','content':new_config}})
                        response=await member.post(f'/api/sessions/{sid}/chat',json={'text':request,'mode':'agent'})
                        self.assertEqual(response.status_code,200,response.text)
                        events=[json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
                        self.assertEqual(config.read_text(),new_config)
                        injected = os.environ.get('FORGE_TEST_INJECTED_HOOK_RUNNER') == '1'
                        self.assertEqual((ws/'global-hook-ran').exists(),not injected)
                        self.assertFalse(bad_marker.exists())
                        self.assertTrue(any(e.get('type')=='error' and e.get('code')=='hook_trust_denied' for e in events),events)
                        result=[e for e in events if e.get('type')=='tool_result' and e.get('name')=='write_file']
                        self.assertEqual(len(result),1,events); self.assertTrue(result[0]['ok'])
                        self.assertIn('not rolled back',result[0]['output'])
                        if not injected:
                            self.assertIn('Hook on_file_change',result[0]['output'])
                        self.assertTrue(result[0].get('hook_trust_denied'))
                        saved=(await member.get(f'/api/sessions/{sid}')).json()['session']['messages']
                        tool_results=[m for m in saved if m.get('role')=='tool' and m.get('tool_call_id')=='fixture-call']
                        self.assertEqual(len(tool_results),1,saved)
                        self.assertIn('not rolled back',tool_results[0]['content'])
                        self.assertFalse(any(e.get('type')=='turn_end' for e in events),events)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),1)
                        # The newly installed hook remains unapproved on the next turn.
                        next_response=await member.post(f'/api/sessions/{sid}/chat',json={'text':request,'mode':'agent'})
                        next_events=[json.loads(line[5:].strip()) for line in next_response.text.splitlines() if line.startswith('data:')]
                        self.assertTrue(any(e.get('type')=='error' and e.get('code')=='hook_trust_denied' for e in next_events),next_events)
                        self.assertEqual((root/'fake-provider-calls').read_text().count('called'),1)
                        self.assertFalse(bad_marker.exists())
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
