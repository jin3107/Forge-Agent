"""Shared subagent routing must bind and recheck the parent's workspace."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import agent as mod, config, hooks
from engine.session import SessionStore, ScopedSessionStore


class SharedBinding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); sessions=root/'sessions'; workspaces=root/'workspaces'
        sessions.mkdir(); workspaces.mkdir(); self.workspaces=workspaces
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=workspaces,CONFIG_FILE=root/'config.json')
        cp.start(); self.addCleanup(cp.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); session=self.store.create(model='test')
        self.agent=mod.Agent(self.store,session.id)
        mod._BG_AGENTS.clear(); self.addCleanup(mod._BG_AGENTS.clear)

    async def test_foreground_shared_child_uses_parent_workspace(self):
        seen=[]
        class Child:
            def __init__(self,store,sid,**kwargs): self.workspace=store.workspace(sid)
            async def send(self,task):
                seen.append(self.workspace.resolve())
                (self.workspace/'child-file').write_text('child')
                yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',Child):
            events=[event async for event in self.agent._run_subagent('call','task',None,'shared')]
        self.assertEqual(seen,[self.agent.workspace.resolve()])
        self.assertEqual((self.agent.workspace/'child-file').read_text(),'child')
        self.assertTrue(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_changed_child_workspace_at_construction_prevents_execution(self):
        elsewhere=self.workspaces/'elsewhere'; elsewhere.mkdir(); calls=[]
        class Child:
            def __init__(self,store,sid,**kwargs): self.workspace=elsewhere
            async def send(self,task):
                calls.append(task)
                yield {'type':'assistant_message','content':'bad'}
        with patch.object(mod,'Agent',Child):
            events=[event async for event in self.agent._run_subagent('call','task',None,'shared')]
        self.assertEqual(calls,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])
        self.assertFalse(any(e['type']=='subagent_started' for e in events))

    async def test_preexisting_symlink_pointer_is_rejected(self):
        victim=self.workspaces/'victim'; victim.write_text('untouched'); real_create=self.store.create
        def create(**kwargs):
            session=real_create(**kwargs)
            (self.workspaces/f'{session.id}.path').symlink_to(victim)
            return session
        try:
            with patch.object(self.store,'create',side_effect=create), \
                 patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as background:
                result=await self.agent.spawn_background('task',isolation='shared')
                await asyncio.sleep(0)
        except (OSError,NotImplementedError):
            self.skipTest('symlink unsupported')
        self.assertFalse(result['ok']); background.assert_not_awaited()
        self.assertEqual(victim.read_text(),'untouched')

    async def test_background_shared_uses_parent_workspace(self):
        seen=[]
        async def run(bg,sid,task,model): seen.append(self.store.workspace(sid).resolve())
        with patch.object(self.agent,'_bg_agent_run',side_effect=run):
            result=await self.agent.spawn_background('task',isolation='shared'); await asyncio.sleep(0)
        self.assertTrue(result['ok']); self.assertEqual(seen,[self.agent.workspace.resolve()])
        self.assertEqual(mod._BG_AGENTS[result['bg_id']]['isolation'],'shared')

    async def test_background_changed_pointer_before_start_denies_child(self):
        elsewhere=self.workspaces/'elsewhere'; elsewhere.mkdir(); done=asyncio.Event(); orig=self.agent._bg_agent_run
        async def run(*args):
            try: await orig(*args)
            finally: done.set()
        with patch.object(self.agent,'_bg_agent_run',side_effect=run), \
             patch.object(mod.Agent,'send',side_effect=AssertionError('child must not run')) as send, \
             patch('engine.bus.send'),patch.object(self.agent,'_notify',new_callable=AsyncMock),patch.object(mod,'log_tool'):
            result=await self.agent.spawn_background('task',isolation='shared'); self.assertTrue(result['ok'])
            (self.workspaces/f"{result['sub_sid']}.path").write_text(str(elsewhere))
            await asyncio.wait_for(done.wait(),2)
        self.assertEqual(mod._BG_AGENTS[result['bg_id']]['status'],'error')
        self.assertIn('workspace',mod._BG_AGENTS[result['bg_id']]['summary'].lower())
        send.assert_not_called()

    async def test_binding_failure_stops_background_and_foreground(self):
        original=Path.write_text
        def fail(path,*args,**kwargs):
            if path.suffix=='.path': raise OSError('private pointer issue')
            return original(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail), \
             patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as background, \
             patch.object(mod,'Agent',side_effect=AssertionError('child must not be created')):
            result=await self.agent.spawn_background('task',isolation='shared')
            events=[event async for event in self.agent._run_subagent('call','task',None,'shared')]
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); self.assertNotIn('private pointer issue',result['output'])
        self.assertEqual(mod._BG_AGENTS,{}); background.assert_not_awaited()
        result=next(e for e in events if e['type']=='subagent_output')
        self.assertFalse(result['ok']); self.assertFalse(any(e['type']=='subagent_started' for e in events))

    async def test_store_returning_wrong_workspace_blocks_both_paths(self):
        elsewhere=self.workspaces/'elsewhere'; elsewhere.mkdir(); actual=self.store.workspace
        def wrong(sid): return actual(sid) if sid==self.agent.session.id else elsewhere
        with patch.object(self.store,'workspace',side_effect=wrong), \
             patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as background, \
             patch.object(mod,'Agent',side_effect=AssertionError('child must not be created')):
            result=await self.agent.spawn_background('task',isolation='shared')
            events=[event async for event in self.agent._run_subagent('call','task',None,'shared')]
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); self.assertEqual(mod._BG_AGENTS,{}); background.assert_not_awaited()
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_bad_isolation_value_does_not_start_child(self):
        with patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as background, \
             patch.object(mod,'Agent',side_effect=AssertionError('child must not be created')):
            result=await self.agent.spawn_background('task',isolation='unexpected')
            events=[event async for event in self.agent._run_subagent('call','task',None,'unexpected')]
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); background.assert_not_awaited()
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])
