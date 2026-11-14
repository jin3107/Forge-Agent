"""Injected stop-hook trust denials must not produce normal turn completion."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks


class TrustStop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.session=SimpleNamespace(id='sid',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.agent=mod.Agent.__new__(mod.Agent)
        self.agent.session=self.session; self.agent.store=self.store
        self.agent.workspace=Path(self.tmp.name); self.agent.model='test'; self.agent.router=None
        self.agent.depth=0; self.agent._bg_child=False
        self.agent._system_message=lambda extra='':{'role':'system','content':'test'}

    async def test_denied_stop_preserves_answer_but_no_successful_turn_end(self):
        async def model(*args):
            yield {'type':'delta','text':'already completed'}
            yield {'type':'finish','reason':'stop'}
        async def event(workspace,sid,name,extra=None):
            if name=='stop': raise hooks.HookTrustDenied()
            return []
        events=[]
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',side_effect=event), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''):
            with self.assertRaises(hooks.HookTrustDenied):
                async for ev in self.agent.send('question',chat_mode='direct'):
                    events.append(ev)
        self.assertTrue(any(e['type']=='assistant_message' and e['content']=='already completed' for e in events))
        self.assertFalse(any(e['type']=='turn_end' for e in events))
        self.assertEqual(self.saved[-1][-1]['content'],'already completed')

    async def test_stop_runs_before_turn_end_for_success_and_ordinary_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.session.messages=[]
                order=[]
                async def model(*args):
                    yield {'type':'delta','text':'done'}
                    yield {'type':'finish','reason':'stop'}
                async def event(workspace,sid,name,extra=None):
                    if name=='stop':
                        order.append('stop')
                        if failure: raise RuntimeError('ordinary failure')
                    return []
                with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
                     patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
                     patch.object(hooks,'run_event',side_effect=event), \
                     patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''):
                    async for ev in self.agent.send('question',chat_mode='direct'):
                        if ev['type']=='turn_end': order.append('turn_end')
                self.assertEqual(order,['stop','turn_end'])

    async def test_background_child_still_skips_stop(self):
        self.agent._bg_child=True
        async def model(*args):
            yield {'type':'delta','text':'done'}
            yield {'type':'finish','reason':'stop'}
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=AssertionError('unexpected event')) as event, \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''):
            events=[e async for e in self.agent.send('question',chat_mode='direct')]
        event.assert_not_awaited()
        self.assertTrue(any(e['type']=='turn_end' for e in events))

    async def test_chat_sse_denial_skips_commit_and_does_not_emit_turn_end(self):
        from server import main
        from engine import autocommit
        self.session.model='test'; self.store.get=lambda sid:self.session
        self.store.workspace=lambda sid:self.agent.workspace
        async def model(*args):
            yield {'type':'delta','text':'answer already produced'}
            yield {'type':'finish','reason':'stop'}
        async def event(workspace,sid,name,extra=None):
            if name=='stop': raise hooks.HookTrustDenied()
            return []
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with patch.object(main,'_db',return_value=self.store), \
             patch.object(main,'Agent',return_value=self.agent), \
             patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
             patch.object(main.config,'has_provider_credentials',return_value=True), \
             patch.object(main.config,'get_auto_compact_chars',return_value=0), \
             patch.object(main.config,'get_auto_commit',return_value=True), \
             patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(hooks,'run_event',side_effect=event), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit:
            response=await main.chat('sid',main.ChatBody(text='question'),request,user=SimpleNamespace(id='owner', role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('answer already produced',output)
        self.assertIn('hook_trust_denied',output)
        self.assertIn('Earlier operations may have run',output)
        self.assertNotIn('"type": "turn_end"',output)
        self.assertIn('"type": "done"',output)
        commit.assert_not_awaited()
