"""Shared-child runtime lineage records, not hook enforcement."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import agent as mod, config, hook_lineage
from engine.session import SessionStore, ScopedSessionStore

class RuntimeLineage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); (root/'sessions').mkdir(); (root/'workspaces').mkdir()
        p=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); s=self.store.create(model='test'); self.parent=mod.Agent(self.store,s.id); self.ws=self.parent.workspace.resolve()
        mod._BG_AGENTS.clear(); self.addCleanup(mod._BG_AGENTS.clear)
    def valid(self,child): return hook_lineage.is_shared_child('owner',self.parent.session.id,child,self.ws)
    async def test_foreground_only_active_during_real_child_execution(self):
        observed=[]
        class Child:
            def __init__(child,store,sid,**kwargs): child.sid=sid; child.workspace=store.workspace(sid)
            async def send(child,task):
                observed.append((child.sid,self.valid(child.sid),hook_lineage.is_shared_child('other',self.parent.session.id,child.sid,self.ws)))
                yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',Child):
            events=[e async for e in self.parent._run_subagent('id','task',None,'shared')]
        child=next(e['session_id'] for e in events if e['type']=='subagent_started')
        self.assertEqual(observed,[(child,True,False)]); self.assertFalse(self.valid(child))
    async def test_foreground_cancellation_removes_lineage(self):
        entered=asyncio.Event(); child_ids=[]
        class Child:
            def __init__(child,store,sid,**kwargs): child.sid=sid; child.workspace=store.workspace(sid)
            async def send(child,task):
                child_ids.append(child.sid); entered.set(); await asyncio.Event().wait()
                yield {'type':'assistant_message','content':'unreachable'}
        async def run(): return [e async for e in self.parent._run_subagent('id','task',None,'shared')]
        with patch.object(mod,'Agent',Child):
            task=asyncio.create_task(run()); await asyncio.wait_for(entered.wait(),2)
            self.assertTrue(self.valid(child_ids[0])); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.valid(child_ids[0]))
    async def test_background_only_active_during_real_child_execution(self):
        entered=asyncio.Event(); release=asyncio.Event(); finished=asyncio.Event(); observed=[]
        class Child:
            def __init__(child,store,sid,**kwargs): child.sid=sid; child.workspace=store.workspace(sid)
            async def send(child,task,**kwargs):
                observed.append(self.valid(child.sid)); entered.set(); await release.wait()
                yield {'type':'assistant_message','content':'done'}
        original=self.parent._bg_agent_run
        async def wrapper(*args):
            try: await original(*args)
            finally: finished.set()
        with patch.object(mod,'Agent',Child), patch.object(self.parent,'_bg_agent_run',side_effect=wrapper), \
             patch('engine.bus.send'),patch.object(self.parent,'_notify',new_callable=AsyncMock),patch.object(mod,'log_tool'):
            result=await self.parent.spawn_background('task',isolation='shared'); self.assertTrue(result['ok'])
            await asyncio.wait_for(entered.wait(),2); child=result['sub_sid']
            self.assertEqual(observed,[True]); self.assertTrue(self.valid(child)); release.set(); await asyncio.wait_for(finished.wait(),2)
        self.assertFalse(self.valid(child))
    async def test_background_cancellation_removes_lineage(self):
        entered=asyncio.Event(); done=asyncio.Event(); tasks=[]
        class Child:
            def __init__(child,store,sid,**kwargs): child.sid=sid; child.workspace=store.workspace(sid)
            async def send(child,task,**kwargs):
                entered.set(); await asyncio.Event().wait()
                yield {'type':'assistant_message','content':'unreachable'}
        original=self.parent._bg_agent_run
        async def wrapper(*args):
            tasks.append(asyncio.current_task())
            try: await original(*args)
            finally: done.set()
        with patch.object(mod,'Agent',Child), patch.object(self.parent,'_bg_agent_run',side_effect=wrapper):
            result=await self.parent.spawn_background('task',isolation='shared'); self.assertTrue(result['ok'])
            await asyncio.wait_for(entered.wait(),2); child=result['sub_sid']; self.assertTrue(self.valid(child))
            tasks[0].cancel(); await asyncio.wait_for(done.wait(),2)
        self.assertFalse(self.valid(child))

    async def test_scope_change_invalidates_active_record(self):
        child=self.store.create(model='test'); self.store.open_workspace(child.id,str(self.ws))
        with hook_lineage.track_shared_child(self.store,self.parent.session.id,child.id,self.ws) as registered:
            self.assertTrue(registered); self.assertTrue(self.valid(child.id))
            self.store.close_workspace(child.id); self.assertFalse(self.valid(child.id))
        self.assertFalse(self.valid(child.id))
    async def test_duplicate_child_cannot_acquire_second_parent_record(self):
        child=self.store.create(model='test'); self.store.open_workspace(child.id,str(self.ws))
        other=self.store.create(model='test'); self.store.open_workspace(other.id,str(self.ws))
        with hook_lineage.track_shared_child(self.store,self.parent.session.id,child.id,self.ws) as first:
            self.assertTrue(first)
            with hook_lineage.track_shared_child(self.store,other.id,child.id,self.ws) as second:
                self.assertFalse(second)
                self.assertFalse(hook_lineage.is_shared_child('owner',other.id,child.id,self.ws))
            self.assertTrue(self.valid(child.id))
        self.assertFalse(self.valid(child.id))

    async def test_worktree_child_never_gets_shared_record(self):
        other=self.ws.parent/'worktree'; other.mkdir(); seen=[]
        async def prepare(sid):
            self.store.open_workspace(sid,str(other)); return str(other.resolve())
        class Child:
            def __init__(child,store,sid,**kwargs): child.sid=sid; child.workspace=store.workspace(sid)
            async def send(child,task):
                seen.append(hook_lineage.is_shared_child('owner',self.parent.session.id,child.sid,other.resolve()))
                yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',Child), patch.object(self.parent,'_prepare_subagent_worktree',side_effect=prepare), \
             patch('engine.worktree.diff_stat_result',new_callable=AsyncMock,return_value={'ok':True,'stat':''}):
            _=[e async for e in self.parent._run_subagent('id','task',None,'worktree')]
        self.assertEqual(seen,[False])

    async def test_local_and_unrelated_sessions_not_issued(self):
        child=self.store.create(model='test'); self.store.open_workspace(child.id,str(self.ws))
        with hook_lineage.track_shared_child(self.store.base,self.parent.session.id,child.id,self.ws) as registered:
            self.assertFalse(registered)
        self.assertFalse(self.valid(child.id))
