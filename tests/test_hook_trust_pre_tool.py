"""Injected pre-tool trust denials stop dispatch; this is not an active gate."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks, tools


def call(name, id, **args):
    return {'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}


class TrustPreTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session=SimpleNamespace(id='sid',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False, save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.agent=mod.Agent.__new__(mod.Agent)
        self.agent.session=self.session
        self.agent.store=self.store
        self.agent.workspace=Path(self.tmp.name)
        self.agent.model='test'
        self.agent.router=None
        self.agent._bg_child=False
        self.agent._system_message=lambda extra='':{'role':'system','content':'test'}

    async def run_calls(self, calls, pre):
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':calls}
            yield {'type':'finish','reason':'stop'}
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,side_effect=pre), \
             patch.object(hooks,'post_tool_use',new_callable=AsyncMock,return_value=''), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock,return_value={'ok':True,'output':'completed'}) as execute, \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                [e async for e in self.agent.send('request')]
            return execute.await_count

    def assert_complete_transcript(self, calls):
        messages=self.saved[-1]
        assistant=next(m for m in messages if m['role']=='assistant' and m.get('tool_calls'))
        self.assertEqual([x['id'] for x in assistant['tool_calls']], [x['id'] for x in calls])
        results=[m for m in messages if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in results], [x['id'] for x in calls])
        self.assertTrue(all(m['content'] for m in results))

    async def test_first_denial_prevents_all_tool_calls(self):
        calls=[call('write_file','a',path='a',content='x'),call('write_file','b',path='b',content='y')]
        count=await self.run_calls(calls, hooks.HookTrustDenied())
        self.assertEqual(count,0)
        self.assert_complete_transcript(calls)
        self.assertFalse((self.agent.workspace/'a').exists())
        self.assertIn('may have',self.saved[-1][-1]['content'])

    async def test_later_denial_preserves_prior_result_and_marks_remaining(self):
        calls=[call('write_file','a',path='a',content='x'),call('write_file','b',path='b',content='y'),call('write_file','c',path='c',content='z')]
        count=await self.run_calls(calls, [None,hooks.HookTrustDenied()])
        self.assertEqual(count,1)
        self.assert_complete_transcript(calls)
        results=[m for m in self.saved[-1] if m['role']=='tool']
        self.assertEqual(results[0]['content'],'completed')
        self.assertIn('may have',results[1]['content'])

    async def test_chat_sse_denial_skips_post_turn_autocommit(self):
        from server import main
        from engine import autocommit
        self.session.model='test'
        self.store.get=lambda sid:self.session
        self.store.workspace=lambda sid:self.agent.workspace
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('write_file','a',path='a',content='x')]}
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
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock) as execute, \
             patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit:
            response=await main.chat('sid',main.ChatBody(text='request'),request,user=SimpleNamespace(id='owner', role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('hook_trust_denied',output)
        self.assertIn('Earlier operations may have run',output)
        self.assertIn('"type": "done"',output)
        execute.assert_not_awaited()
        commit.assert_not_awaited()
        self.assert_complete_transcript([call('write_file','a',path='a',content='x')])

    async def test_fanout_denial_before_dispatch_starts_no_children(self):
        calls=[call('spawn_subagent','a',task='a'),call('spawn_subagent','b',task='b')]
        with patch.object(self.agent,'_run_subagent',side_effect=AssertionError('spawned')):
            count=await self.run_calls(calls,[None,hooks.HookTrustDenied()])
        self.assertEqual(count,0)
        self.assert_complete_transcript(calls)
