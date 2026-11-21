"""Prototype detects persistent revocation after the final hook, without rollback."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks, hook_checked_runner as checked
from engine.hook_approvals import Decision, Snapshot

class FinalRecheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name)

    async def invoke(self,kind,code,revoked):
        event={'general':'stop','pre':'pre_tool_use','post':'post_tool_use'}[kind]
        raw=json.dumps({'hooks':{event:'marker'}}).encode()
        digest=hashlib.sha256(raw).hexdigest()
        pair=(Decision('approved',digest,'admin'),Snapshot('hooks',digest,raw))
        state={'valid':True,'calls':0}
        def resolve(*_):
            state['calls']+=1
            return pair if state['valid'] else None
        async def run(*args,**kwargs):
            (self.ws/'marker').write_text('ran')
            if revoked: state['valid']=False
            return {'code':code,'out':'done','err':'reason'}
        with patch.object(hooks,'_run_cmd',side_effect=run):
            if kind=='general':
                result=await checked.run_checked_event(self.ws,'owner','sid',event,{},resolve,{})
            elif kind=='pre':
                result=await checked.checked_pre_tool_use(self.ws,'owner','sid','write_file',{},resolve,{})
            else:
                result=await checked.checked_post_tool_use(self.ws,'owner','sid','write_file',{},'tool output',resolve,{})
        return result,state

    async def test_revocation_during_last_command_denies_all_paths(self):
        for kind,code in [('general',0),('pre',0),('pre',2),('post',0)]:
            with self.subTest(kind=kind,code=code):
                (self.ws/'marker').unlink(missing_ok=True)
                with self.assertRaises(hooks.HookTrustDenied):
                    await self.invoke(kind,code,True)
                self.assertEqual((self.ws/'marker').read_text(),'ran')

    async def test_unchanged_approval_returns_normal_results(self):
        for kind,code in [('general',0),('pre',0),('pre',2),('post',0)]:
            with self.subTest(kind=kind,code=code):
                result,state=await self.invoke(kind,code,False)
                self.assertGreaterEqual(state['calls'],2)
                if kind=='pre' and code==2: self.assertTrue(result['blocked'])
                if kind=='pre' and code==0: self.assertIsNone(result)
                if kind=='post': self.assertIn('reason',result)
