"""Injected trust denials at turn start must not be swallowed.

The production hook runner does not yet raise these denials.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine import agent as agent_module, hooks, memory


class TrustTurnStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved=[]
        self.session=SimpleNamespace(id='sid', title='Existing', messages=[{'role':'user','content':'previous'}])
        self.agent=agent_module.Agent.__new__(agent_module.Agent)
        self.agent.session=self.session
        self.agent.store=SimpleNamespace(save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.agent.workspace=Path(self.tmp.name)
        self.agent.model='test'
        self.agent._bg_child=False

    async def test_prompt_denial_preserves_input_and_stops_before_other_work(self):
        for mode in ('agent','direct','plan','architect'):
            with self.subTest(mode=mode):
                self.session.messages=[{'role':'user','content':'previous'}]
                with patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()) as submit, \
                     patch.object(hooks,'session_start_cache',new_callable=AsyncMock) as start, \
                     patch.object(agent_module.config,'get_auto_compact_chars',side_effect=AssertionError('compaction checked')), \
                     patch.object(self.agent,'_stream_chat',side_effect=AssertionError('provider called')):
                    with self.assertRaises(hooks.HookTrustDenied):
                        [event async for event in self.agent.send('new prompt',chat_mode=mode)]
                self.assertEqual(submit.await_args.args[2], 'user_prompt_submit')
                start.assert_not_awaited()
                self.assertEqual(self.session.messages[-1],{'role':'user','content':'new prompt'})
                self.assertEqual(self.saved[-1],self.session.messages)

    async def test_prompt_denial_prevents_quick_memory_write(self):
        with patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(memory,'quick_memory',side_effect=AssertionError('memory write')):
            with self.assertRaises(hooks.HookTrustDenied):
                [event async for event in self.agent.send('# secret note')]
        self.assertEqual(self.session.messages[-1]['content'],'# secret note')

    async def test_ordinary_hook_errors_keep_existing_nonfatal_behavior(self):
        with patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=RuntimeError('ordinary error')), \
             patch.object(memory,'quick_memory',return_value='saved') as quick:
            events=[event async for event in self.agent.send('# note')]
        quick.assert_called_once()
        self.assertEqual(events[0]['content'],'saved')
        with patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,side_effect=RuntimeError('ordinary error')), \
             patch.object(agent_module.config,'get_auto_compact_chars',return_value=0), \
             patch.object(self.agent,'_system_message',side_effect=RuntimeError('system reached')):
            with self.assertRaisesRegex(RuntimeError,'system reached'):
                [event async for event in self.agent.send('next')]

    async def test_session_start_denial_stops_modes_and_rerun_or_background(self):
        for mode, append, background in [('agent',True,False),('direct',True,False),
                                         ('plan',True,False),('architect',True,False),
                                         ('agent',False,False),('agent',True,True)]:
            with self.subTest(mode=mode,append=append,background=background):
                self.session.messages=[{'role':'user','content':'previous'}]
                self.agent._bg_child=background
                with patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]) as prompt, \
                     patch.object(hooks,'session_start_cache',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()) as start, \
                     patch.object(agent_module.config,'get_auto_compact_chars',return_value=0), \
                     patch.object(self.agent,'_system_message',side_effect=AssertionError('system prompt built')), \
                     patch.object(self.agent,'_stream_chat',side_effect=AssertionError('provider called')):
                    with self.assertRaises(hooks.HookTrustDenied):
                        [event async for event in self.agent.send('new prompt',chat_mode=mode,append_user=append)]
                start.assert_awaited_once()
                self.assertEqual(prompt.await_count, int(append and not background))
                self.assertEqual(self.session.messages[-1]['content'], 'new prompt' if append else 'previous')

    async def test_chat_sse_reports_both_denials_without_auto_commit(self):
        from server import main
        from engine import autocommit
        self.session.model='test'
        self.agent.store.get=lambda sid:self.session
        self.agent.store.workspace=lambda sid:self.agent.workspace
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        user=SimpleNamespace(id='owner', role='admin')
        for phase in ('prompt','session_start'):
            with self.subTest(phase=phase):
                self.session.messages=[{'role':'user','content':'previous'}]
                self.agent._bg_child=False
                failure=hooks.HookTrustDenied()
                with patch.object(main,'_db',return_value=self.agent.store), \
                     patch.object(main,'Agent',return_value=self.agent), \
                     patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
                     patch.object(main.config,'has_provider_credentials',return_value=True), \
                     patch.object(main.config,'get_auto_compact_chars',return_value=0), \
                     patch.object(main.config,'get_auto_commit',return_value=True), \
                     patch.object(hooks,'run_event',new_callable=AsyncMock,side_effect=failure if phase == 'prompt' else None,return_value=[]), \
                     patch.object(hooks,'session_start_cache',new_callable=AsyncMock,side_effect=failure if phase == 'session_start' else None), \
                     patch.object(autocommit,'commit_turn',new_callable=AsyncMock) as commit:
                    response=await main.chat('sid',main.ChatBody(text='new prompt'),request,user=user)
                    output=''.join([chunk async for chunk in response.body_iterator])
                self.assertIn('hook_trust_denied',output)
                self.assertIn('"type": "done"',output)
                self.assertEqual(self.session.messages[-1]['content'],'new prompt')
                commit.assert_not_awaited()
