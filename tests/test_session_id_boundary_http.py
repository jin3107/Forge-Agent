"""Authenticated HTTP regression: sanitized session aliases must not escape workspace."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parent.parent
HARNESS=ROOT/'tests'/'fixtures'/'http_background_harness.py'
PASSWORD='fixture-password-at-least-16-characters'

@unittest.skipUnless(os.name=='posix','POSIX fixture process lifecycle')
class SessionIDBoundaryHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_owned_session_alias_cannot_read_or_write_sibling_directory(self):
        with tempfile.TemporaryDirectory(prefix='forge-session-alias-') as td:
            root=Path(td); env=os.environ.copy()
            for k in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                      'FORGE_TEST_DEMO','FORGE_TEST_HOOK_ADAPTER','FORGE_TEST_GLOBAL_LEGACY'):
                env.pop(k,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),FORGE_TEST_ADD_MEMBER='1',PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
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
                    url=await asyncio.wait_for(ready(),15)
                    async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as c:
                        c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                        login=await c.post('/api/auth/login',json={'email':'member@example.test','password':PASSWORD})
                        self.assertEqual(login.status_code,200,login.text); c.headers['X-CSRF-Token']=login.json()['csrf_token']
                        sid=(await c.post('/api/sessions',json={})).json()['session']['id']
                        outside=root/'data'/sid; outside.mkdir(); (outside/'sentinel.txt').write_text('private sentinel')
                        ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'sentinel.txt').write_text('own workspace')
                        ok=await c.get('/api/workspace/file',params={'sid':sid,'path':'sentinel.txt'})
                        self.assertEqual(ok.status_code,200,ok.text); self.assertEqual(ok.json()['content'],'own workspace')
                        alias='../'+sid
                        read=await c.get('/api/workspace/file',params={'sid':alias,'path':'sentinel.txt'})
                        write=await c.put('/api/workspace/file',params={'sid':alias},json={'path':'sentinel.txt','content':'overwritten'})
                        self.assertEqual((read.status_code,write.status_code),(404,404),(read.text,write.text))
                        self.assertEqual((outside/'sentinel.txt').read_text(),'private sentinel')
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
