"""Injected child trust denial must not become a successful serial delegation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks


class TrustSerialSubagent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.session=SimpleNamespace(id='parent',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)),
                                   create=lambda **kwargs:SimpleNamespace(id='child'), workspace=lambda sid:self.root)
        self.parent=mod.Agent.__new__(mod.Agent)
        self.parent.session=self.session; self.parent.store=self.store
        self.parent.workspace=self.root; self.parent.model='test'; self.parent.router=None
        self.parent.depth=0; self.parent._bg_child=False
        self.parent._system_message=lambda extra='':{'role':'system','content':'test'}
        self.parent._resolve_sub_model=lambda *args:None
        async def send(*args,**kwargs):
            yield {'type':'tool_result','name':'write_file','ok':True,'output':'done'}
            raise hooks.HookTrustDenied()
        self.child=SimpleNamespace(send=send,workspace=self.root)

    async def test_child_denial_escapes_wrapper_without_success_summary(self):
        events=[]
        with patch.object(mod,'Agent',return_value=self.child), patch.object(mod.config,'WORKSPACES_DIR',self.root):
            with self.assertRaises(hooks.HookTrustDenied):
                async for event in self.parent._run_subagent('call','child task',None):
                    events.append(event)
        self.assertTrue(any(e['type']=='subagent_started' for e in events))
        self.assertTrue(any(e['type']=='subagent_progress' for e in events))
        self.assertFalse(any(e['type'] in ('subagent_done','subagent_output') for e in events))

    async def test_ordinary_child_exception_remains_reported_as_before(self):
        async def ordinary(*args,**kwargs):
            if False:
                yield {}
            raise RuntimeError('ordinary child failure')
        self.child.send=ordinary
        with patch.object(mod,'Agent',return_value=self.child), patch.object(mod.config,'WORKSPACES_DIR',self.root):
            events=[event async for event in self.parent._run_subagent('call','child task',None)]
        self.assertTrue(any(e['type']=='subagent_done' for e in events))
        self.assertIn('ordinary child failure',next(e['output'] for e in events if e['type']=='subagent_output'))

    async def test_serial_child_denial_reaches_chat_stream_and_skips_commit(self):
        from server import main
        from engine import autocommit
        self.session.model='test'
        self.store.get=lambda sid:self.session
        self.store.workspace=lambda sid:self.root
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':[{'id':'call','type':'function','function':{'name':'spawn_subagent','arguments':json.dumps({'task':'child task'})}}]}
            yield {'type':'finish','reason':'stop'}
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with patch.object(main,'_db',return_value=self.store), \
             patch.object(main,'Agent',return_value=self.parent), \
             patch.object(mod,'Agent',return_value=self.child), \
             patch.object(mod.config,'WORKSPACES_DIR',self.root), \
             patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
             patch.object(main.config,'has_provider_credentials',return_value=True), \
             patch.object(main.config,'get_auto_compact_chars',return_value=0), \
             patch.object(main.config,'get_auto_commit',return_value=True), \
             patch.object(self.parent,'_llm_with_fallback',side_effect=model), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit, \
             patch.object(mod,'log_tool'):
            response=await main.chat('parent',main.ChatBody(text='request'),request,user=SimpleNamespace(id='owner', role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('hook_trust_denied',output)
        self.assertIn('Earlier operations may have run',output)
        self.assertNotIn('subagent_done',output)
        commit.assert_not_awaited()

    async def test_parent_turn_stops_and_records_interrupted_call(self):
        calls=[{'id':'call','type':'function','function':{'name':'spawn_subagent',
                'arguments':json.dumps({'task':'child task'})}}]
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':calls}
            yield {'type':'finish','reason':'stop'}
        events=[]
        with patch.object(mod,'Agent',return_value=self.child), \
             patch.object(mod.config,'WORKSPACES_DIR',self.root), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(self.parent,'_llm_with_fallback',side_effect=model) as provider, \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            with self.assertRaises(hooks.HookTrustDenied):
                async for event in self.parent.send('request'):
                    events.append(event)
        self.assertEqual(provider.call_count,1)
        self.assertFalse(any(e['type']=='subagent_done' for e in events))
        self.assertFalse(any(e['type']=='tool_result' and e.get('ok') for e in events))
        results=[m for m in self.saved[-1] if m['role']=='tool']
        self.assertEqual(len(results),1)
        self.assertEqual(results[0]['tool_call_id'],'call')
        self.assertIn('may have run',results[0]['content'])
