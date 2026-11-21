"""Fan-out must propagate injected trust denials and settle sibling pumps.

No production hook gate is active; child tasks and the provider are simulated.
"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks


def call(id, isolation='shared'):
    return {'id':id,'type':'function','function':{'name':'spawn_subagent',
            'arguments':json.dumps({'task':id,'isolation':isolation})}}


class TrustFanout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.session=SimpleNamespace(id='parent',owner_id='owner',title='Existing',messages=[],usage={})
        self.saved=[]
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:self.saved.append(copy.deepcopy(s.messages)))
        self.parent=mod.Agent.__new__(mod.Agent)
        self.parent.session=self.session; self.parent.store=self.store
        self.parent.workspace=Path(self.tmp.name); self.parent.model='test'; self.parent.router=None
        self.parent.depth=0; self.parent._bg_child=False
        self.parent._system_message=lambda extra='':{'role':'system','content':'test'}
        self.parent._resolve_sub_model=lambda *args:None

    async def test_child_denial_cancels_and_joins_siblings_and_stops_later_chunk(self):
        entered=asyncio.Event(); cancelled=asyncio.Event(); started=[]
        async def child(id,task,model,isolation):
            started.append(id)
            if id=='a':
                await entered.wait()
                raise hooks.HookTrustDenied()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            if False:
                yield {}
        calls=[call(x) for x in ('a','b','c','d')]
        events=[]
        with patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            async def run():
                async for ev in self.parent._handle_parallel_spawns(self.session,[],calls,{'spawns':0}):
                    events.append(ev)
            with self.assertRaises(hooks.HookTrustDenied):
                await asyncio.wait_for(run(),1)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn('d',started)
        self.assertFalse(any(e['type']=='tool_result' and e.get('ok') for e in events))

    async def test_generator_close_cancels_started_children(self):
        entered=asyncio.Event(); cancelled=asyncio.Event()
        async def child(id,task,model,isolation):
            entered.set()
            try:
                yield {'type':'subagent_progress','id':id}
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        calls=[call('a'),call('b')]
        # Closing after a child completion/progress while a sibling is running.
        # The pump buffers progress until child completion in the existing design.
        async def mixed(id,task,model,isolation):
            if id=='a':
                await entered.wait()
                yield {'type':'subagent_progress','id':id}
                return
            async for event in child(id,task,model,isolation):
                yield event
        with patch.object(self.parent,'_run_subagent',side_effect=mixed), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            stream=self.parent._handle_parallel_spawns(self.session,[],calls,{'spawns':0})
            async def consume():
                async for ev in stream:
                    if ev['type']=='subagent_progress':
                        await stream.aclose()
                        return
            await asyncio.wait_for(consume(),1)
        self.assertTrue(cancelled.is_set())

    async def test_cancelling_fanout_wait_settles_child_pumps(self):
        started=asyncio.Event(); cancelled=[]
        async def child(id,task,model,isolation):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(id)
            if False: yield {}
        with patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            async def run():
                return [e async for e in self.parent._handle_parallel_spawns(self.session,[],[call('a'),call('b')],{'spawns':0})]
            task=asyncio.create_task(run())
            await asyncio.wait_for(started.wait(),1)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task,1)
        self.assertEqual(set(cancelled),{'a','b'})

    async def test_ordinary_child_errors_keep_existing_result_behavior(self):
        async def child(id,task,model,isolation):
            if id=='a':
                raise RuntimeError('ordinary failure')
            yield {'type':'subagent_output','id':id,'output':'ok'}
        with patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            events=[e async for e in self.parent._handle_parallel_spawns(self.session,[],[call('a'),call('b')],{'spawns':0})]
        outputs={e['id']:e for e in events if e['type']=='tool_result'}
        self.assertIn('ordinary failure',outputs['a']['output'])
        self.assertTrue(outputs['b']['ok'])

    async def test_explicit_child_failure_is_not_reported_as_success(self):
        async def child(id,task,model,isolation):
            yield {'type':'subagent_output','id':id,'output':'worktree unavailable','ok':id!='a'}
        with patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            events=[e async for e in self.parent._handle_parallel_spawns(self.session,[],[call('a','worktree'),call('b')],{'spawns':0})]
        outputs={e['id']:e for e in events if e['type']=='tool_result'}
        self.assertFalse(outputs['a']['ok']); self.assertTrue(outputs['b']['ok'])

    async def test_each_child_receives_its_own_isolation(self):
        seen={}
        async def child(id,task,model,isolation):
            seen[id]=isolation
            yield {'type':'subagent_output','id':id,'output':'ok'}
        calls=[call('a','shared'),call('b','worktree')]
        with patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            events=[ev async for ev in self.parent._handle_parallel_spawns(self.session,[],calls,{'spawns':0})]
        self.assertEqual(seen,{'a':'shared','b':'worktree'})
        self.assertEqual(sum(e['type']=='tool_result' and e['ok'] for e in events),2)

    async def test_parent_loop_propagates_child_denial_and_closes_transcript(self):
        calls=[call('a'),call('b')]
        async def model(*args):
            yield {'type':'tool_calls','tool_calls':calls}
            yield {'type':'finish','reason':'stop'}
        async def child(id,task,model,isolation):
            if id=='a':
                raise hooks.HookTrustDenied()
            await asyncio.Event().wait()
            if False: yield {}
        with patch.object(self.parent,'_llm_with_fallback',side_effect=model), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(self.parent,'_run_subagent',side_effect=child), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            async def run():
                return [e async for e in self.parent.send('request')]
            with self.assertRaises(hooks.HookTrustDenied):
                await asyncio.wait_for(run(),1)
        results=[m for m in self.saved[-1] if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in results],['a','b'])
        self.assertTrue(all('may have run' in m['content'] for m in results))
