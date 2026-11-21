"""Injected notification trust denial stops approvals and reports background completion."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, approvals, hooks, tools


def call(name, args):
    return {'id':'call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}


class TrustNotification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.session=SimpleNamespace(id='parent',owner_id='owner',title='Existing',messages=[],usage={},allowed_commands=[])
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.agent=mod.Agent.__new__(mod.Agent)
        self.agent.session=self.session; self.agent.store=self.store
        self.agent.workspace=Path(self.tmp.name); self.agent.model='test'; self.agent.router=None
        self.agent.depth=0; self.agent._bg_child=False
        self.agent._system_message=lambda extra='':{'role':'system','content':'test'}
        approvals.PENDING.clear()
        self.addCleanup(approvals.PENDING.clear)

    async def test_notification_denial_stops_before_creating_command_approval(self):
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('run_command',{'command':'dangerous'})]}
            yield {'type':'finish','reason':'stop'}
        async def event(workspace,sid,name,extra=None):
            if name=='notification': raise hooks.HookTrustDenied()
            return []
        observed=[]
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(mod.config,'load_config',return_value={'approval_mode':'ask'}), \
             patch.object(mod.guard,'assess_command',return_value={'level':'warn','reasons':['test']}), \
             patch.object(hooks,'run_event',side_effect=event), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(approvals,'create',wraps=approvals.create) as create, \
             patch.object(approvals,'wait',new_callable=AsyncMock,return_value='deny'), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock) as execute, \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                async for ev in self.agent.send('request'):
                    observed.append(ev)
        create.assert_not_called(); execute.assert_not_awaited()
        self.assertEqual(approvals.PENDING,{})
        self.assertFalse(any(e['type']=='approval_request' for e in observed))
        result=next(m for m in self.saved[-1] if m['role']=='tool')
        self.assertIn('may have run',result['content'])

    async def test_notification_denial_stops_before_creating_mcp_approval(self):
        from engine import connectors
        self.store.allow_mcp=True
        spec={'type':'function','function':{'name':'mcp_fixture','description':'x','parameters':{'type':'object','properties':{}}}}
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('mcp_fixture',{})]}
            yield {'type':'finish','reason':'stop'}
        async def event(workspace,sid,name,extra=None):
            if name=='notification': raise hooks.HookTrustDenied()
            return []
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(mod.config,'load_config',return_value={'approval_mode':'ask'}), \
             patch.object(mod.mcp_manager,'get_tool_specs',new_callable=AsyncMock,return_value=([spec],{'mcp_fixture':(SimpleNamespace(name='srv'),'tool')})), \
             patch.object(mod.mcp_manager,'call_namespaced',new_callable=AsyncMock) as invoke, \
             patch.object(connectors,'tool_allowed',return_value=False), \
             patch.object(hooks,'run_event',side_effect=event), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(approvals,'create',wraps=approvals.create) as create, \
             patch.object(approvals,'wait',new_callable=AsyncMock,return_value='deny'), \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                [ev async for ev in self.agent.send('request')]
        create.assert_not_called(); invoke.assert_not_awaited()
        self.assertEqual(approvals.PENDING,{})

    async def test_chat_sse_reports_notification_denial_without_autocommit(self):
        from server import main
        from engine import autocommit
        self.session.model='test'; self.store.get=lambda sid:self.session
        self.store.workspace=lambda sid:self.agent.workspace
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[call('run_command',{'command':'dangerous'})]}
            yield {'type':'finish','reason':'stop'}
        async def event(workspace,sid,name,extra=None):
            if name=='notification': raise hooks.HookTrustDenied()
            return []
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with patch.object(main,'_db',return_value=self.store), \
             patch.object(main,'Agent',return_value=self.agent), \
             patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
             patch.object(main.config,'has_provider_credentials',return_value=True), \
             patch.object(main.config,'get_auto_compact_chars',return_value=0), \
             patch.object(main.config,'get_auto_commit',return_value=True), \
             patch.object(mod.config,'load_config',return_value={'approval_mode':'ask'}), \
             patch.object(mod.guard,'assess_command',return_value={'level':'warn','reasons':['test']}), \
             patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(hooks,'run_event',side_effect=event), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(tools,'execute_tool',new_callable=AsyncMock) as execute, \
             patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit, \
             patch.object(mod,'log_tool'):
            response=await main.chat('parent',main.ChatBody(text='request'),request,user=SimpleNamespace(id='owner', role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('hook_trust_denied',output)
        self.assertNotIn('approval_request',output)
        self.assertEqual(approvals.PENDING,{})
        execute.assert_not_awaited(); commit.assert_not_awaited()

    async def test_background_completion_records_notification_denial(self):
        from engine import bus
        async def child_send(*args,**kwargs):
            yield {'type':'assistant_message','content':'completed work'}
        child=SimpleNamespace(send=child_send,_bg_child=False)
        mod._BG_AGENTS['bg-test']={'status':'running','summary':'','lead_sid':'parent'}
        self.addCleanup(mod._BG_AGENTS.pop,'bg-test',None)
        async def event(workspace,sid,name,extra=None):
            if name=='notification': raise hooks.HookTrustDenied()
            return []
        with patch.object(mod,'Agent',return_value=child), patch.object(hooks,'run_event',side_effect=event), \
             patch.object(bus,'send') as send, patch.object(mod,'log_tool'):
            await self.agent._bg_agent_run('bg-test','child','work',None)
        entry=mod._BG_AGENTS['bg-test']
        self.assertEqual(entry['status'],'done')
        self.assertEqual(entry['summary'],'completed work')
        self.assertEqual(entry['notification_status'],'hook_trust_denied')
        self.assertTrue(any('notification' in str(c.args).lower() for c in send.call_args_list))

    async def test_ordinary_notification_errors_remain_nonfatal(self):
        with patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=RuntimeError('ordinary failure')):
            self.assertIsNone(await self.agent._notify('test','message'))
