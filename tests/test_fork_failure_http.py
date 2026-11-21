"""Authenticated fork endpoint reports snapshot export failure rather than false success."""
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

@unittest.skipUnless(os.name=='posix','POSIX fixture lifecycle')
class ForkFailureHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_fork_bad_snapshot_returns_409_and_no_live_copy(self):
        with tempfile.TemporaryDirectory(prefix='forge-fork-fail-') as td:
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
                    async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as member, \
                               httpx.AsyncClient(base_url=url,trust_env=False,timeout=20) as other:
                        async def login(c,email):
                            c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                            r=await c.post('/api/auth/login',json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code,200,r.text); c.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test'); await login(other,'member2@example.test')
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        wss=root/'data'/'workspaces'; ws=wss/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'file').write_text('live')
                        self.assertEqual((await other.post(f'/api/sessions/{sid}/fork')).status_code,404)
                        (wss/f'.ckpt-{sid}.git').mkdir()
                        failed=await member.post(f'/api/sessions/{sid}/fork')
                        self.assertEqual(failed.status_code,409,failed.text); self.assertIn('child session',failed.json()['detail'])
                        rows=(await member.get('/api/sessions')).json()['sessions']
                        children=[row['id'] for row in rows if row['id']!=sid]; self.assertEqual(len(children),1,rows)
                        self.assertIn(children[0],failed.json()['detail']); self.assertFalse((wss/children[0]/'file').exists())
                        self.assertEqual((ws/'file').read_text(),'live')
                        sid2=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws2=wss/sid2; ws2.mkdir(exist_ok=True); (ws2/'file').write_text('fallback')
                        valid=await member.post(f'/api/sessions/{sid2}/fork'); self.assertEqual(valid.status_code,200,valid.text)
                        self.assertEqual((wss/valid.json()['session']['id']/'file').read_text(),'fallback')
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
