"""Experimental checked session-start cache; production runner remains unchanged."""
import asyncio
import hashlib
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import hooks
from engine.hook_approvals import Decision, Snapshot
from engine.hook_review_requests import ReviewRequests
from server import main


@unittest.skipUnless(os.name=='posix','shell quoting tested on POSIX only')
class CheckedSessionCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.config=self.ws/'.forge'/'config.json'; self.registry=ReviewRequests()
        self.admin=SimpleNamespace(enabled=True,role='admin')
        from engine.hook_checked_runner import CheckedSessionStartCache
        self.cache=CheckedSessionStartCache(capacity=2)

    def write(self,cmd):
        raw=json.dumps({'hooks':{'session_start':cmd}}).encode(); self.config.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def approve(self,digest):
        row=self.registry.submit('owner','sid',str(self.ws),digest,share_commands=True)
        self.assertEqual(self.registry.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True),'approved')
        return row

    def resolve(self,owner,sid,ws):
        return main._hook_scope_snapshot(owner,sid,str(ws))

    def patches(self):
        return patch.multiple(main,_hook_review_requests=self.registry,_current_requested_workspace=lambda row:Path(row.workspace))

    async def get(self,workspace=None,owner='owner',sid='sid',global_config=None,resolver=None):
        return await self.cache.run(workspace or self.ws,owner,sid,resolver or self.resolve,global_config or {})

    def cmd(self,marker):
        script=f"from pathlib import Path; p=Path({str(marker)!r}); p.open('a').write('x'); print('context')"
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    async def test_hit_rechecks_approval_and_new_approval_does_not_reuse_old_cache(self):
        marker=self.ws/'marker'; digest=self.write(self.cmd(marker)); row=self.approve(digest)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertEqual(await self.get(),'context')
            self.assertEqual(await self.get(),'context')
            self.assertEqual(marker.read_text(),'x')
            self.registry.revoke(row.id,'owner',False)
            with self.assertRaises(hooks.HookTrustDenied): await self.get()
            self.assertEqual(marker.read_text(),'x')
            new=self.approve(digest); self.assertNotEqual(row.id,new.id)
            self.assertEqual(await self.get(),'context')
        self.assertEqual(marker.read_text(),'xx')

    async def test_changed_config_or_disabled_admin_blocks_cache_hit(self):
        digest=self.write(self.cmd(self.ws/'marker')); self.approve(digest)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            await self.get()
            self.config.write_bytes(b'{')
            with self.assertRaises(hooks.HookTrustDenied): await self.get()
        digest=self.write(self.cmd(self.ws/'marker')); self.approve(digest)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=SimpleNamespace(enabled=False,role='admin')):
            with self.assertRaises(hooks.HookTrustDenied): await self.get()

    async def test_no_hooks_global_cache_changes_with_global_selection(self):
        self.config.write_bytes(b'{}')
        one=self.ws/'one'; two=self.ws/'two'
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            await self.get(global_config={'hooks':{'session_start':self.cmd(one)}})
            await self.get(global_config={'hooks':{'session_start':self.cmd(one)}})
            await self.get(global_config={'hooks':{'session_start':self.cmd(two)}})
        self.assertEqual(one.read_text(),'x'); self.assertEqual(two.read_text(),'x')

    async def test_observed_invalid_config_clears_hook_free_global_cache(self):
        marker=self.ws/'global'; self.config.write_bytes(b'{}')
        cfg={'hooks':{'session_start':self.cmd(marker)}}
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            await self.get(global_config=cfg)
            self.config.write_bytes(b'{')
            with self.assertRaises(hooks.HookTrustDenied): await self.get(global_config=cfg)
            self.config.write_bytes(b'{}')
            await self.get(global_config=cfg)
        self.assertEqual(marker.read_text(),'xx')

    async def test_simultaneous_cache_misses_may_both_execute(self):
        entered=asyncio.Event(); release=asyncio.Event(); count=[0]
        async def run(command,payload,workspace):
            count[0]+=1
            if count[0]==2: entered.set()
            await release.wait()
            return {'code':0,'out':'context','err':''}
        resolver=lambda *_:(Decision('absent'),Snapshot('absent',None,None))
        cfg={'hooks':{'session_start':'noop'}}
        with patch.object(hooks,'_run_cmd',side_effect=run):
            first=asyncio.create_task(self.get(resolver=resolver,global_config=cfg))
            second=asyncio.create_task(self.get(resolver=resolver,global_config=cfg))
            try:
                await asyncio.wait_for(entered.wait(),1)
                release.set()
                self.assertEqual(await asyncio.gather(first,second),['context','context'])
                await self.get(resolver=resolver,global_config=cfg)
                self.assertEqual(count[0],2)
            finally:
                release.set(); await asyncio.gather(first,second,return_exceptions=True)

    async def test_revoke_during_hook_prevents_cache_and_return(self):
        digest=self.write(self.cmd(self.ws/'marker')); row=self.approve(digest)
        original=hooks._run_cmd
        async def run_and_revoke(*args,**kwargs):
            out=await original(*args,**kwargs); self.registry.revoke(row.id,'owner',False); return out
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin),patch.object(hooks,'_run_cmd',side_effect=run_and_revoke):
            with self.assertRaises(hooks.HookTrustDenied): await self.get()
        self.assertTrue((self.ws/'marker').exists())
        self.assertEqual(len(self.cache),0)

    async def test_approved_snapshot_without_approval_identity_is_not_cacheable(self):
        raw=json.dumps({'hooks':{'session_start':'noop'}}).encode(); digest=hashlib.sha256(raw).hexdigest()
        resolver=lambda *_:(Decision('approved',digest),Snapshot('hooks',digest,raw))
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock) as execute:
            with self.assertRaises(hooks.HookTrustDenied): await self.get(resolver=resolver)
            execute.assert_not_awaited()

    async def test_late_old_miss_does_not_replace_new_workspace_cache(self):
        a=self.ws/'a'; b=self.ws/'b'; a.mkdir(); b.mkdir()
        entered=asyncio.Event(); release=asyncio.Event()
        async def run(command,payload,workspace):
            if command=='first': entered.set(); await release.wait()
            return {'code':0,'out':command,'err':''}
        def resolver(owner,sid,ws): return Decision('absent'),Snapshot('absent',None,None)
        with patch.object(hooks,'_run_cmd',side_effect=run) as execute:
            old=asyncio.create_task(self.get(a,global_config={'hooks':{'session_start':'first'}},resolver=resolver))
            await asyncio.wait_for(entered.wait(),1)
            try:
                self.assertEqual(await self.get(b,global_config={'hooks':{'session_start':'second'}},resolver=resolver),'second')
                release.set(); self.assertEqual(await old,'first')
                self.assertEqual(await self.get(b,global_config={'hooks':{'session_start':'second'}},resolver=resolver),'second')
                self.assertEqual(execute.await_count,2)
            finally:
                release.set(); await asyncio.gather(old,return_exceptions=True)

    async def test_capacity_is_bounded_and_scope_separated(self):
        resolver=lambda *_:(Decision('absent'),Snapshot('absent',None,None))
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':0,'out':'x','err':''}) as run:
            for sid in ('one','two','three'):
                await self.get(sid=sid,resolver=resolver,global_config={'hooks':{'session_start':'noop'}})
            self.assertEqual(len(self.cache),2)
            await self.get(sid='one',resolver=resolver,global_config={'hooks':{'session_start':'noop'}})
            self.assertEqual(run.await_count,4)
