"""Authenticated REST and synthetic chat worktree merge use workspace undo history."""
import asyncio
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from engine import checkpoints, config, worktree

ROOT=Path(__file__).resolve().parent.parent
HARNESS=ROOT/'tests'/'fixtures'/'http_background_harness.py'
PASSWORD='fixture-password-at-least-16-characters'

@unittest.skipUnless(shutil.which('git') and os.name=='posix','requires Git and POSIX harness')
class WorktreeMergeCheckpointHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_rest_and_chat_merge_can_be_undone(self):
        with tempfile.TemporaryDirectory(prefix='forge-merge-http-') as td:
            root=Path(td); env=os.environ.copy()
            for k in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL',
                      'FORGE_TEST_DEMO','FORGE_TEST_HOOK_ADAPTER','FORGE_TEST_ADD_MEMBER'):
                env.pop(k,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
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
                        login=await c.post('/api/auth/login',json={'email':'alice@example.test','password':PASSWORD})
                        self.assertEqual(login.status_code,200,login.text); c.headers['X-CSRF-Token']=login.json()['csrf_token']
                        for mode in ('rest','chat'):
                            sid=(await c.post('/api/sessions',json={})).json()['session']['id']
                            child=(await c.post('/api/sessions',json={})).json()['session']['id']
                            ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'file.txt').write_text('original\n')
                            with patch.multiple(config,DATA_DIR=root/'data',WORKSPACES_DIR=root/'data'/'workspaces',SESSIONS_DIR=root/'data'/'sessions',CONFIG_FILE=root/'data'/'config.json'):
                                self.assertIsNotNone(await checkpoints.snapshot(sid,ws,'base'))
                                wt=await worktree.make(sid,child); self.assertIsNotNone(wt)
                                (Path(wt)/'file.txt').write_text('merged\n')
                                repo=worktree._repo(sid)
                                rc, before=await worktree._run('--git-dir',str(repo),'rev-parse','refs/heads/forge'); self.assertEqual(rc,0)
                            if mode=='rest':
                                response=await c.post(f'/api/worktrees/{child}/merge',params={'sid':sid})
                                self.assertEqual(response.status_code,200,response.text); self.assertTrue(response.json()['ok'],response.text)
                                self.assertTrue(response.json()['checkpoint_created'])
                            else:
                                response=await c.post(f'/api/sessions/{sid}/chat',json={'text':f'/worktree merge {child}','mode':'agent'})
                                self.assertEqual(response.status_code,200,response.text); self.assertIn('Đã gộp',response.text)
                            self.assertEqual((ws/'file.txt').read_text(),'merged\n')
                            undo=await c.post(f'/api/sessions/{sid}/undo')
                            self.assertEqual(undo.status_code,200,undo.text); self.assertTrue(undo.json()['ok'],undo.text)
                            self.assertEqual((ws/'file.txt').read_text(),'original\n')
                            with patch.object(config,'WORKSPACES_DIR',root/'data'/'workspaces'):
                                rc, after=await worktree._run('--git-dir',str(repo),'rev-parse','refs/heads/forge')
                                self.assertEqual(rc,0); self.assertEqual(before,after)
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError: proc.kill(); await proc.wait()
                    if proc.returncode not in (0,-signal.SIGTERM): print((root/'server.log').read_text())
