"""A future hook trust denial must not be swallowed by compaction callers.

These tests inject the denial; they do not activate a hook execution gate.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine import agent as agent_module, hooks


class TrustCompaction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = SimpleNamespace(id='sid', title='Existing', messages=[
            {'role':'user', 'content':f'old message {i}'} for i in range(9)])
        self.store = SimpleNamespace(save=lambda session: None)
        self.agent = agent_module.Agent.__new__(agent_module.Agent)
        self.agent.session = self.session
        self.agent.store = self.store
        self.agent.workspace = Path(self.tmp.name)
        self.agent.model = 'test'
        self.agent._bg_child = False

    async def test_compaction_denial_propagates_and_keeps_messages(self):
        before = copy.deepcopy(self.session.messages)
        with patch.object(hooks, 'run_event', new_callable=AsyncMock, side_effect=hooks.HookTrustDenied()), \
             patch.object(self.agent, '_stream_chat', side_effect=AssertionError('provider called')):
            with self.assertRaises(hooks.HookTrustDenied):
                await agent_module.compact_session(self.agent)
        self.assertEqual(self.session.messages, before)

    async def test_existing_hook_exit_two_and_other_errors_keep_existing_behavior(self):
        before = copy.deepcopy(self.session.messages)
        with patch.object(hooks, 'run_event', new_callable=AsyncMock, return_value=[{'rc':2}]):
            result = await agent_module.compact_session(self.agent)
        self.assertFalse(result['ok'])
        self.assertEqual(self.session.messages, before)
        async def stream(*args, **kwargs):
            yield {'type':'delta', 'text':'summary'}
        with patch.object(hooks, 'run_event', new_callable=AsyncMock, side_effect=RuntimeError('ordinary hook failure')), \
             patch.object(self.agent, '_stream_chat', side_effect=stream):
            result = await agent_module.compact_session(self.agent)
        self.assertTrue(result['ok'])
        self.assertIn('summary', self.session.messages[0]['content'])

    async def test_auto_compaction_denial_stops_turn_preserving_new_prompt(self):
        async def events(workspace, sid, event, extra=None):
            if event == 'pre_compact':
                raise hooks.HookTrustDenied()
            return []
        with patch.object(hooks, 'run_event', side_effect=events), \
             patch.object(agent_module.config, 'get_auto_compact_chars', return_value=1), \
             patch.object(hooks, 'session_start_cache', new_callable=AsyncMock) as session_start, \
             patch.object(self.agent, '_stream_chat', side_effect=AssertionError('provider called')):
            observed=[]
            with self.assertRaises(hooks.HookTrustDenied):
                async for event in self.agent.send('new request'):
                    observed.append(event)
        self.assertTrue(any(e['type']=='compacting' for e in observed))
        self.assertEqual(self.session.messages[-1], {'role':'user','content':'new request'})
        self.assertEqual(len(self.session.messages), 10)
        session_start.assert_not_awaited()

    async def test_manual_endpoint_returns_structured_denial(self):
        from server import main
        db = SimpleNamespace(get=lambda sid:self.session)
        with patch.object(main, '_db', return_value=db), patch.object(main, 'Agent', return_value=self.agent), \
             patch.object(agent_module, 'compact_session', new_callable=AsyncMock, side_effect=hooks.HookTrustDenied()):
            result = await main.compact_session_ep('sid', user=SimpleNamespace(id='owner', role='admin'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'hook_trust_denied')

    async def test_chat_stream_reports_denial_and_skips_autocommit(self):
        from server import main
        from engine import autocommit
        self.session.model = 'test'
        self.store.get = lambda sid:self.session
        self.store.workspace = lambda sid:self.agent.workspace
        async def hook_event(workspace, sid, event, extra=None):
            if event == 'pre_compact':
                raise hooks.HookTrustDenied()
            return []
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        user = SimpleNamespace(id='owner', role='admin')
        with patch.object(main, '_db', return_value=self.store), \
             patch.object(main, 'Agent', return_value=self.agent), \
             patch.object(main.model_registry, 'route', return_value=SimpleNamespace(id='test')), \
             patch.object(main.config, 'has_provider_credentials', return_value=True), \
             patch.object(main.config, 'get_auto_compact_chars', return_value=1), \
             patch.object(main.config, 'get_auto_commit', return_value=True), \
             patch.object(hooks, 'run_event', side_effect=hook_event), \
             patch.object(autocommit, 'commit_turn', new_callable=AsyncMock) as commit:
            response = await main.chat('sid', main.ChatBody(text='new request'), request, user=user)
            output = ''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('hook_trust_denied', output)
        self.assertIn('"type": "done"', output)
        self.assertEqual(self.session.messages[-1]['content'], 'new request')
        commit.assert_not_awaited()
