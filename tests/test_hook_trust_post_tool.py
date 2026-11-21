"""Injected post-tool trust denials preserve real results and stop the turn."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks, tools


def call(id):
    return {'id':id,'type':'function','function':{'name':'write_file','arguments':json.dumps({'path':id,'content':'written'})}}


class TrustPostTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session=SimpleNamespace(id='sid',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.agent=mod.Agent.__new__(mod.Agent)
        self.agent.session=self.session; self.agent.store=self.store
        self.agent.workspace=Path(self.tmp.name); self.agent.model='test'; self.agent.router=None
        self.agent._bg_child=False
        self.agent._system_message=lambda extra='':{'role':'system','content':'test'}

    async def test_post_denial_preserves_actual_tool_result_and_stops_next(self):
        calls=[call('first'),call('second')]
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':calls}
            yield {'type':'finish','reason':'stop'}
        async def execute(name,args,workspace,**kwargs):
            (workspace/'marker').write_text('side effect')
            return {'ok':True,'output':'actual result','path':'first','diff':'actual diff'}
        events=[]
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model) as provider, \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock,side_effect=execute) as execute_mock, \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                async for event in self.agent.send('request'):
                    events.append(event)
        self.assertEqual(execute_mock.await_count,1)
        self.assertEqual(provider.call_count,1)
        self.assertEqual((self.agent.workspace/'marker').read_text(),'side effect')
        result=next(e for e in events if e['type']=='tool_result')
        self.assertTrue(result['ok'])
        self.assertEqual(result['path'],'first')
        self.assertEqual(result['diff'],'actual diff')
        self.assertTrue(result['hook_trust_denied'])
        self.assertIn('actual result',result['output'])
        self.assertIn('not rolled back',result['output'])
        stored=[m for m in self.saved[-1] if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in stored],['first','second'])
        self.assertEqual(stored[0]['content'],result['output'])
        self.assertIn('may have run',stored[1]['content'])

    async def test_chat_sse_reports_actual_result_and_error_without_autocommit(self):
        from server import main
        from engine import autocommit
        self.session.model='test'
        self.store.get=lambda sid:self.session
        self.store.workspace=lambda sid:self.agent.workspace
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('first'),call('second')]}
            yield {'type':'finish','reason':'stop'}
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with patch.object(main,'_db',return_value=self.store), \
             patch.object(main,'Agent',return_value=self.agent), \
             patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
             patch.object(main.config,'has_provider_credentials',return_value=True), \
             patch.object(main.config,'get_auto_compact_chars',return_value=0), \
             patch.object(main.config,'get_auto_commit',return_value=True), \
             patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock,return_value={'ok':True,'output':'completed'}) as execute, \
             patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit, \
             patch.object(mod,'log_tool'):
            response=await main.chat('sid',main.ChatBody(text='request'),request,user=SimpleNamespace(id='owner', role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('completed',output)
        self.assertIn('not rolled back',output)
        self.assertIn('hook_trust_denied',output)
        self.assertIn('"type": "done"',output)
        execute.assert_awaited_once()
        commit.assert_not_awaited()

    async def test_save_failure_does_not_convert_denial_to_successful_turn(self):
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('first')]}
            yield {'type':'finish','reason':'stop'}
        count=[0]
        def save(session):
            count[0]+=1
            if count[0] > 1:
                raise OSError('private storage failure')
        self.store.save=save
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock,return_value={'ok':True,'output':'completed'}) as execute, \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                [e async for e in self.agent.send('request')]
        execute.assert_awaited_once()
        self.assertGreaterEqual(count[0],2)

    async def test_closed_stream_already_persisted_completed_result(self):
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('first'),call('second')]}
            yield {'type':'finish','reason':'stop'}
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock,return_value={'ok':False,'output':'failed result'}), \
             patch.object(mod,'log_tool'):
            stream=self.agent.send('request')
            async for event in stream:
                if event['type']=='tool_result':
                    self.assertFalse(event['ok'])
                    await stream.aclose()
                    break
        stored=[m for m in self.saved[-1] if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in stored],['first','second'])
        self.assertIn('failed result',stored[0]['content'])
        self.assertIn('not rolled back',stored[0]['content'])
