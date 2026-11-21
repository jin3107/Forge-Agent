"""Scoped checked runner adapter: explicit dependencies, not product wiring."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import hooks
from engine.hook_approvals import Decision, Snapshot
from server.hook_runner import ScopedCheckedHookRunner

class ScopedRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); raw=json.dumps({'hooks':{'stop':'echo ok','session_start':'echo hello'}}).encode()
        digest=hashlib.sha256(raw).hexdigest(); self.pair=(Decision('approved',digest,'admin-review'),Snapshot('hooks',digest,raw))
        self.calls=[]; self.valid=True
    def resolve(self,owner,sid,ws):
        self.calls.append((owner,sid,ws)); return self.pair if self.valid else None
    def make(self,**kwargs):
        args={'owner_id':'owner','resolve':self.resolve,'load_config':lambda:{}}
        args.update(kwargs); return ScopedCheckedHookRunner(**args)
    def test_rejects_invalid_constructor_dependencies(self):
        for override in ({'owner_id':''},{'owner_id':'  '},{'resolve':None},{'load_config':None}):
            with self.subTest(override=override),self.assertRaises(ValueError): self.make(**override)
    async def test_bound_owner_fresh_resolve_and_cached_start_rechecks(self):
        runner=self.make()
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':0,'out':'ok','err':''}) as run:
            self.assertEqual(await runner.session_start_cache(self.ws,'sid'),'ok')
            self.assertEqual(await runner.session_start_cache(self.ws,'sid'),'ok')
            self.assertEqual(run.await_count,1)
            self.assertTrue(all(c[0]=='owner' and c[1]=='sid' and c[2]==self.ws for c in self.calls))
            self.valid=False
            with self.assertRaises(hooks.HookTrustDenied): await runner.session_start_cache(self.ws,'sid')
            with self.assertRaises(hooks.HookTrustDenied): await runner.run_event(self.ws,'sid','stop')
            self.assertEqual(run.await_count,1)
    async def test_fresh_global_config_each_call_and_independent_runner_cache(self):
        calls=[]; absent=(Decision('absent'),Snapshot('absent',None,None))
        def load(): calls.append('load'); return {'hooks':{'session_start':'echo global'}}
        one=self.make(resolve=lambda *_:absent,load_config=load); two=self.make(resolve=lambda *_:absent,load_config=load)
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':0,'out':'ok','err':''}) as run:
            await one.session_start_cache(self.ws,'sid'); await one.session_start_cache(self.ws,'sid'); await two.session_start_cache(self.ws,'sid')
            self.assertEqual(run.await_count,2); self.assertGreaterEqual(len(calls),3)
    async def test_pre_and_post_delegate_to_checked_runner(self):
        raw=json.dumps({'hooks':{'pre_tool_use':'pre','post_tool_use':'post'}}).encode()
        digest=hashlib.sha256(raw).hexdigest(); self.pair=(Decision('approved',digest,'review'),Snapshot('hooks',digest,raw))
        runner=self.make()
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':0,'out':'info','err':''}) as run:
            self.assertIsNone(await runner.pre_tool_use(self.ws,'sid','write_file',{'path':'x'}))
            result=await runner.post_tool_use(self.ws,'sid','write_file',{'path':'x'},'output')
            self.assertIn('info',result); self.assertEqual(run.await_count,2)
            self.valid=False
            with self.assertRaises(hooks.HookTrustDenied):
                await runner.pre_tool_use(self.ws,'sid','write_file',{})
            with self.assertRaises(hooks.HookTrustDenied):
                await runner.post_tool_use(self.ws,'sid','write_file',{},'output')
            self.assertEqual(run.await_count,2)

    async def test_loader_failure_denies_without_private_message(self):
        def bad(): raise OSError('private loader error')
        runner=self.make(load_config=bad)
        with self.assertRaises(hooks.HookTrustDenied) as caught: await runner.run_event(self.ws,'sid','stop')
        self.assertNotIn('private loader error',str(caught.exception))
