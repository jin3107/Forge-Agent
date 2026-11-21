"""Background child trust denials are recorded, not reported as success."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks


class BackgroundTrust(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.session=SimpleNamespace(id='lead',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.parent=mod.Agent.__new__(mod.Agent)
        self.parent.session=self.session; self.parent.store=self.store
        self.parent.workspace=self.root; self.parent.model='test'; self.parent.router=None
        self.parent.depth=0; self.parent._bg_child=False
        self.parent._system_message=lambda extra='':{'role':'system','content':'test'}
        mod._BG_AGENTS.clear(); self.addCleanup(mod._BG_AGENTS.clear)
        self.entry={'bg_id':'bga-test','lead_sid':'lead','sub_sid':'child','task':'work','status':'running','summary':'','tools':0}
        mod._BG_AGENTS['bga-test']=self.entry

    async def test_denial_before_or_after_work_records_error_and_warns(self):
        from engine import bus
        for work in (False,True):
            with self.subTest(work=work):
                self.entry.update(status='running',summary='',tools=0)
                self.entry.pop('error_code',None)
                marker=self.root/'marker'
                marker.unlink(missing_ok=True)
                async def child_send(*args,**kwargs):
                    if work:
                        marker.write_text('side effect')
                        yield {'type':'tool_result','name':'write_file','ok':True}
                    raise hooks.HookTrustDenied()
                child=SimpleNamespace(send=child_send,_bg_child=False)
                with patch.object(mod,'Agent',return_value=child), patch.object(bus,'send') as send, \
                     patch.object(self.parent,'_notify',new_callable=AsyncMock) as notify, \
                     patch.object(mod,'log_tool') as log:
                    await self.parent._bg_agent_run('bga-test','child','work',None)
                self.assertEqual(self.entry['status'],'error')
                self.assertEqual(self.entry['error_code'],'hook_trust_denied')
                self.assertEqual(self.entry['tools'],int(work))
                self.assertEqual(marker.exists(),work)
                self.assertIn('Earlier operations may have run',self.entry['summary'])
                self.assertIn('hook_trust_denied',str(send.call_args.args))
                self.assertFalse(log.call_args.args[3])
                notify.assert_awaited_once()
                self.assertTrue(child._bg_child)

    async def test_real_child_send_propagates_injected_session_start_denial(self):
        from engine import bus
        child_session=SimpleNamespace(id='child',owner_id='owner',title='Child',messages=[],usage={})
        saved=[]
        child=mod.Agent.__new__(mod.Agent)
        child.session=child_session
        child.store=SimpleNamespace(save=lambda s:saved.append(copy.deepcopy(s.messages)))
        child.workspace=self.root; child.model='test'; child.router=None; child.depth=1; child._bg_child=False
        with patch.object(mod,'Agent',return_value=child), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(child,'_system_message',side_effect=AssertionError('should not proceed')), \
             patch.object(bus,'send'),patch.object(self.parent,'_notify',new_callable=AsyncMock), \
             patch.object(mod,'log_tool'):
            await self.parent._bg_agent_run('bga-test','child','work',None)
        self.assertEqual(self.entry['error_code'],'hook_trust_denied')
        self.assertEqual(saved[-1][-1]['content'],'work')

    async def test_delivery_failure_does_not_erase_error_or_skip_notification(self):
        from engine import bus
        async def child_send(*args,**kwargs):
            if False: yield {}
            raise hooks.HookTrustDenied()
        child=SimpleNamespace(send=child_send,_bg_child=False)
        with patch.object(mod,'Agent',return_value=child), \
             patch.object(bus,'send',side_effect=OSError('private bus error')), \
             patch.object(self.parent,'_notify',new_callable=AsyncMock) as notify, \
             patch.object(mod,'log_tool'):
            await self.parent._bg_agent_run('bga-test','child','work',None)
        self.assertEqual(self.entry['status'],'error')
        self.assertEqual(self.entry['error_code'],'hook_trust_denied')
        self.assertEqual(self.entry['delivery_status'],'failed')
        notify.assert_awaited_once()

    async def test_check_background_agents_exposes_error_code(self):
        self.entry.update(status='error',error_code='hook_trust_denied',summary='trust failure',
                          notification_status='hook_trust_denied', delivery_status='failed')
        calls=[{'id':'c','type':'function','function':{'name':'check_background_agents','arguments':json.dumps({})}}]
        count=[0]
        async def model(*args):
            count[0]+=1
            if count[0]==1:
                yield {'type':'tool_calls','tool_calls':calls}
            else:
                yield {'type':'delta','text':'done'}
            yield {'type':'finish','reason':'stop'}
        with patch.object(self.parent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,return_value=''), \
             patch.object(mod,'log_tool'):
            events=[e async for e in self.parent.send('status')]
        output=next(e['output'] for e in events if e['type']=='tool_result')
        self.assertIn('code=hook_trust_denied',output)
        self.assertIn('notification=hook_trust_denied',output)
        self.assertIn('delivery=failed',output)
        self.assertIn('trust failure',output)

    async def test_ordinary_child_error_remains_error_without_trust_code(self):
        from engine import bus
        async def child_send(*args,**kwargs):
            if False: yield {}
            raise RuntimeError('ordinary child failure')
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=child_send)), \
             patch.object(bus,'send'),patch.object(self.parent,'_notify',new_callable=AsyncMock), \
             patch.object(mod,'log_tool'):
            await self.parent._bg_agent_run('bga-test','child','work',None)
        self.assertEqual(self.entry['status'],'error')
        self.assertNotIn('error_code',self.entry)
        self.assertIn('ordinary child failure',self.entry['summary'])
