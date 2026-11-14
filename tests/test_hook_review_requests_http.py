"""Explicit member-to-admin hook review requests over authenticated HTTP."""
import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT/'tests'/'fixtures'/'http_background_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'

class ReviewRequestsHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_member_request_is_explicit_scoped_revocable_and_not_approval(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL','FORGE_TEST_BACKGROUND_READ_FAILURE','FORGE_TEST_BACKGROUND_GROUP_FAILURE'):
                env.pop(key, None)
            env.update(FORGE_DATA_DIR=str(root/'data'), FORGE_TEST_ADD_MEMBER='1', PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            with (root/'server.log').open('w+') as log:
                proc = await asyncio.create_subprocess_exec(sys.executable,'-u',str(HARNESS),str(root),cwd=str(ROOT),env=env,stdout=log,stderr=log)
                try:
                    async def ready():
                        while True:
                            if proc.returncode is not None: raise AssertionError((root/'server.log').read_text())
                            try:
                                base='http://127.0.0.1:'+(root/'port').read_text()
                                async with httpx.AsyncClient(trust_env=False,timeout=.5) as c:
                                    if (await c.get(base+'/api/health')).status_code == 200: return base
                            except (OSError,httpx.HTTPError): pass
                            await asyncio.sleep(.03)
                    base=await asyncio.wait_for(ready(),15)
                    async with httpx.AsyncClient(base_url=base,trust_env=False) as member, httpx.AsyncClient(base_url=base,trust_env=False) as admin, httpx.AsyncClient(base_url=base,trust_env=False) as other, httpx.AsyncClient(base_url=base,trust_env=False) as member2, httpx.AsyncClient(base_url=base,trust_env=False) as anon:
                        async def login(c,email):
                            c.headers['X-CSRF-Token']=(await c.get('/api/auth/me')).json()['csrf_token']
                            r=await c.post('/api/auth/login',json={'email':email,'password':PASSWORD})
                            self.assertEqual(r.status_code,200,r.text)
                            c.headers['X-CSRF-Token']=r.json()['csrf_token']
                        await login(member,'member@example.test')
                        await login(admin,'alice@example.test')
                        await login(other,'bob@example.test')
                        await login(member2,'member2@example.test')
                        member_id=(await member.get('/api/auth/me')).json()['user']['id']
                        sid=(await member.post('/api/sessions',json={})).json()['session']['id']
                        ws=root/'data'/'workspaces'/sid
                        ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir(exist_ok=True)
                        marker=ws/'not-executed'
                        config=ws/'.forge'/'config.json'
                        config.write_text(json.dumps({'hooks':{'stop':f'touch {marker}'}}))
                        preview=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        data={'sid':sid,'expected_workspace':preview['workspace'],'expected_digest':preview['digest'], 'share_commands':True}
                        url='/api/workspace/hook-review-requests'
                        self.assertEqual((await admin.get('/api/sessions/'+sid)).status_code,404)
                        self.assertEqual((await admin.get(url)).json()['requests'],[])
                        self.assertEqual((await member.post(url,json=data,headers={'X-CSRF-Token':'wrong'})).status_code,403)
                        self.assertEqual((await other.post(url,json=data)).status_code,404)
                        self.assertEqual((await anon.get(url)).status_code,401)
                        invalid=await member.post(url,json={**data,'expected_digest':'0'*64})
                        self.assertEqual(invalid.status_code,409,invalid.text)
                        invalid=await member.post(url,json={**data,'expected_workspace':str(root)})
                        self.assertEqual(invalid.status_code,409,invalid.text)
                        self.assertEqual((await admin.get(url)).json()['requests'],[])
                        no_consent = {k:v for k,v in data.items() if k != 'share_commands'}
                        self.assertEqual((await member.post(url,json=no_consent)).status_code,422)
                        self.assertEqual((await member.post(url,json={**data,'share_commands':False})).status_code,422)
                        self.assertEqual((await member.post(url,json={**data,'share_commands':1})).status_code,422)
                        sent=await member.post(url,json=data)
                        self.assertEqual(sent.status_code,200,sent.text)
                        self.assertEqual(sent.headers['cache-control'],'no-store')
                        result=sent.json(); self.assertFalse(result['enforcement_active'])
                        rid=result['request_id']
                        self.assertEqual((await member2.get(url)).json()['requests'],[])
                        self.assertEqual((await member2.delete(url+'/'+rid)).status_code,404)
                        self.assertEqual((await member2.post(url,json=data)).status_code,404)
                        self.assertEqual((await member.get(url)).json()['requests'][0]['request_id'],rid)
                        duplicate=await member.post(url,json=data)
                        self.assertEqual(duplicate.json()['request_id'],rid)
                        listed=await admin.get(url)
                        self.assertEqual(listed.headers['cache-control'],'no-store')
                        rows=listed.json()['requests']; self.assertEqual(len(rows),1)
                        self.assertEqual(rows[0]['request_id'],rid)
                        self.assertEqual(rows[0]['workspace'],str(ws.resolve()))
                        self.assertEqual(rows[0]['digest'],preview['digest'])
                        self.assertFalse(rows[0]['snapshot_revalidated'])
                        self.assertNotIn('touch ',listed.text)
                        self.assertFalse(listed.json()['enforcement_active'])
                        detail_url = url+'/'+rid
                        self.assertEqual((await member.get(detail_url)).status_code,403)
                        self.assertEqual((await member2.get(detail_url)).status_code,403)
                        detail = await admin.get(detail_url)
                        self.assertEqual(detail.status_code,200,detail.text)
                        self.assertEqual(detail.headers['cache-control'],'no-store')
                        self.assertEqual(detail.json()['hooks']['stop'], f'touch {marker}')
                        self.assertEqual(detail.json()['digest'],preview['digest'])
                        self.assertFalse(detail.json()['enforcement_active'])
                        decision_url = detail_url+'/decision'
                        decision = {'decision':'approve', 'expected_workspace':preview['workspace'], 'expected_digest':preview['digest']}
                        self.assertEqual((await member.post(decision_url,json=decision)).status_code,403)
                        self.assertEqual((await admin.post(decision_url,json=decision,headers={'X-CSRF-Token':'wrong'})).status_code,403)
                        self.assertEqual((await admin.post(decision_url,json={**decision,'expected_digest':'0'*64})).status_code,409)
                        accepted=await admin.post(decision_url,json=decision)
                        self.assertEqual(accepted.status_code,200,accepted.text)
                        self.assertEqual(accepted.json()['status'],'approved')
                        self.assertFalse(accepted.json()['enforcement_active'])
                        self.assertEqual((await admin.get(url)).json()['requests'][0]['status'],'approved')
                        self.assertEqual((await admin.post(decision_url,json={**decision,'decision':'deny'})).status_code,409)
                        self.assertFalse(marker.exists())
                        other_ws=root/'switched'; other_ws.mkdir(); (other_ws/'.forge').mkdir()
                        (other_ws/'.forge'/'config.json').write_text(config.read_text())
                        pointer = root/'data'/'workspaces'/f'{sid}.path'
                        pointer.write_text(str(other_ws))
                        switched=await admin.get(detail_url)
                        self.assertEqual(switched.status_code,409,switched.text)
                        self.assertNotIn('touch ',switched.text)
                        pointer.unlink()
                        self.assertEqual((await admin.get(detail_url)).status_code,404, 'observed workspace switch invalidates old request')
                        rid2=(await member.post(url,json=data)).json()['request_id']
                        detail2=url+'/'+rid2
                        disabled=await admin.patch('/api/auth/users/'+member_id,json={'enabled':False})
                        self.assertEqual(disabled.status_code,200,disabled.text)
                        self.assertEqual((await admin.get(detail2)).status_code,409)
                        enabled=await admin.patch('/api/auth/users/'+member_id,json={'enabled':True})
                        self.assertEqual(enabled.status_code,200,enabled.text)
                        await login(member,'member@example.test')
                        self.assertEqual((await admin.get(detail2)).status_code,404, 'observed disabled owner invalidates old request')
                        rid3=(await member.post(url,json=data)).json()['request_id']
                        detail3=url+'/'+rid3
                        config.write_text(json.dumps({'hooks':{'stop':'echo changed'}}))
                        stale_detail = await admin.get(detail3)
                        self.assertEqual(stale_detail.status_code,409,stale_detail.text)
                        self.assertNotIn('echo changed',stale_detail.text)
                        self.assertEqual((await admin.post(detail3+'/decision',json=decision)).status_code,404)
                        changed=await member.post(url,json=data)
                        self.assertEqual(changed.status_code,409)
                        new=(await member.get('/api/workspace/hook-review',params={'sid':sid})).json()
                        replacement=await member.post(url,json={**data,'expected_digest':new['digest']})
                        self.assertNotEqual(replacement.json()['request_id'],rid)
                        self.assertEqual((await admin.get(detail_url)).status_code,404)
                        new_detail_url = url+'/'+replacement.json()['request_id']
                        self.assertEqual((await admin.get(new_detail_url)).json()['hooks']['stop'],'echo changed')
                        self.assertEqual((await admin.get(url)).json()['requests'][0]['request_id'],replacement.json()['request_id'])
                        deny=await admin.post(new_detail_url+'/decision',json={'decision':'deny','expected_workspace':new['workspace'],'expected_digest':new['digest']})
                        self.assertEqual(deny.status_code,200,deny.text)
                        self.assertEqual(deny.json()['status'],'denied')
                        self.assertFalse(deny.json()['enforcement_active'])
                        self.assertEqual((await member.delete(url+'/'+replacement.json()['request_id'],headers={'X-CSRF-Token':'wrong'})).status_code,403)
                        self.assertEqual((await member.delete(url+'/'+replacement.json()['request_id'])).status_code,200)
                        self.assertEqual((await admin.get(new_detail_url)).status_code,404)
                        self.assertEqual((await admin.get(url)).json()['requests'],[])
                finally:
                    if proc.returncode is None:
                        proc.send_signal(signal.SIGTERM)
                        try: await asyncio.wait_for(proc.wait(),10)
                        except asyncio.TimeoutError:
                            proc.kill(); await proc.wait()
