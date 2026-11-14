"""Background worktree setup must fail closed before scheduling a child."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import agent as mod, config, hooks, worktree
from engine.session import SessionStore, ScopedSessionStore


class BackgroundWorktreeBinding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); sessions=root/'sessions'; workspaces=root/'workspaces'
        sessions.mkdir(); workspaces.mkdir(); self.workspaces=workspaces
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=workspaces,CONFIG_FILE=root/'config.json')
        cp.start(); self.addCleanup(cp.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); session=self.store.create(model='test')
        self.agent=mod.Agent(self.store,session.id)
        mod._BG_AGENTS.clear(); self.addCleanup(mod._BG_AGENTS.clear)

    async def test_missing_worktree_does_not_schedule_child(self):
        with patch.object(worktree,'make',new_callable=AsyncMock,return_value=None), \
             patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as run:
            result=await self.agent.spawn_background('task',isolation='worktree')
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); self.assertIn('worktree',result['output'].lower())
        self.assertEqual(mod._BG_AGENTS,{}); run.assert_not_awaited()

    async def test_pointer_write_failure_does_not_schedule_child(self):
        wt=self.workspaces/'worktree'; wt.mkdir(); original=Path.write_text
        def fail(path,*args,**kwargs):
            if path.suffix=='.path': raise OSError('private failure')
            return original(path,*args,**kwargs)
        with patch.object(worktree,'make',new_callable=AsyncMock,return_value=str(wt)), \
             patch.object(Path,'write_text',fail),patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as run:
            result=await self.agent.spawn_background('task',isolation='worktree')
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); self.assertNotIn('private failure',result['output'])
        self.assertEqual(mod._BG_AGENTS,{}); run.assert_not_awaited()

    async def test_overlapping_worktree_and_binding_mismatch_are_denied(self):
        wt=self.workspaces/'worktree'; wt.mkdir()
        for path in (str(self.agent.workspace),str(wt/'missing')):
            with self.subTest(path=path),patch.object(worktree,'make',new_callable=AsyncMock,return_value=path), \
                 patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as run:
                result=await self.agent.spawn_background('task',isolation='worktree')
                await asyncio.sleep(0)
                self.assertFalse(result['ok']); run.assert_not_awaited(); self.assertEqual(mod._BG_AGENTS,{})
        real=self.store.workspace
        def mismatch(sid):
            if sid!=self.agent.session.id: return self.agent.workspace
            return real(sid)
        with patch.object(self.store,'workspace',side_effect=mismatch), \
             patch.object(worktree,'make',new_callable=AsyncMock,return_value=str(wt)), \
             patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as run:
            result=await self.agent.spawn_background('task',isolation='worktree')
            await asyncio.sleep(0)
        self.assertFalse(result['ok']); run.assert_not_awaited(); self.assertEqual(mod._BG_AGENTS,{})

    async def test_valid_worktree_binding_is_passed_to_scheduled_child(self):
        wt=self.workspaces/'worktree'; wt.mkdir()
        seen=[]
        async def child(bg,sid,task,model): seen.append(self.store.workspace(sid).resolve())
        with patch.object(worktree,'make',new_callable=AsyncMock,return_value=str(wt)), \
             patch.object(self.agent,'_bg_agent_run',side_effect=child):
            result=await self.agent.spawn_background('task',isolation='worktree')
            await asyncio.sleep(0)
        self.assertTrue(result['ok']); self.assertEqual(seen,[wt.resolve()])
        self.assertEqual(mod._BG_AGENTS[result['bg_id']]['isolation'],'worktree')

    async def test_changed_pointer_before_background_start_does_not_run_child(self):
        wt=self.workspaces/'worktree'; wt.mkdir(); done=asyncio.Event()
        original=self.agent._bg_agent_run
        async def run_and_signal(*args):
            try: await original(*args)
            finally: done.set()
        with patch.object(worktree,'make',new_callable=AsyncMock,return_value=str(wt)), \
             patch.object(self.agent,'_bg_agent_run',side_effect=run_and_signal), \
             patch.object(mod.Agent,'send',side_effect=AssertionError('child must not run')) as send, \
             patch('engine.bus.send'),patch.object(self.agent,'_notify',new_callable=AsyncMock), \
             patch.object(mod,'log_tool'):
            result=await self.agent.spawn_background('task',isolation='worktree')
            self.assertTrue(result['ok'])
            (self.workspaces/f"{result['sub_sid']}.path").write_text(str(self.agent.workspace))
            await asyncio.wait_for(done.wait(),2)
        self.assertEqual(mod._BG_AGENTS[result['bg_id']]['status'],'error')
        self.assertIn('worktree',mod._BG_AGENTS[result['bg_id']]['summary'].lower())
        send.assert_not_called()

    async def test_trust_denial_propagates_without_starting_child(self):
        with patch.object(worktree,'make',new_callable=AsyncMock,side_effect=hooks.HookTrustDenied()), \
             patch.object(self.agent,'_bg_agent_run',new_callable=AsyncMock) as run:
            with self.assertRaises(hooks.HookTrustDenied):
                await self.agent.spawn_background('task',isolation='worktree')
            await asyncio.sleep(0)
        self.assertEqual(mod._BG_AGENTS,{}); run.assert_not_awaited()
