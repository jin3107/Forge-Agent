"""Subagent workspace routing characterization; mocked worktree factory, no sandbox claim."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import agent as mod, config, worktree
from engine.session import SessionStore, ScopedSessionStore


class SubagentWorkspaceBinding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); sessions=root/'sessions'; workspaces=root/'workspaces'
        sessions.mkdir(); workspaces.mkdir(); self.workspaces=workspaces
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=workspaces,CONFIG_FILE=root/'config.json')
        cp.start(); self.addCleanup(cp.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); s=self.store.create(model='test')
        self.parent=mod.Agent(self.store,s.id); self.parent.workspace.mkdir(exist_ok=True)

    async def run_child(self,make_result):
        destinations=[]
        class FakeChild:
            def __init__(self,store,sid,**kwargs):
                self.ws=store.workspace(sid); self.workspace=self.ws; destinations.append(self.ws.resolve())
            async def send(self,task):
                (self.ws/'child-marker').write_text('child ran')
                yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',FakeChild), \
             patch.object(worktree,'make',new_callable=AsyncMock,return_value=make_result) as make, \
             patch.object(worktree,'diff_stat_result',new_callable=AsyncMock,return_value={'ok':True,'stat':'one file'}):
            events=[ev async for ev in self.parent._run_subagent('call','task',None,'worktree')]
        make.assert_awaited_once()
        return events,destinations

    @unittest.skipUnless(shutil.which('git'),'Git required')
    async def test_real_git_worktree_uses_session_keyed_checkpoint(self):
        from engine import checkpoints
        (self.parent.workspace/'tracked.txt').write_text('parent')
        outcome={}
        snap=await checkpoints.snapshot(self.parent.session.id,self.parent.workspace,'test',outcome=outcome)
        self.assertIsNotNone(snap,outcome)
        destinations=[]
        class FakeChild:
            def __init__(self,store,sid,**kwargs):
                self.workspace=store.workspace(sid); destinations.append(self.workspace)
            async def send(self,task):
                (self.workspace/'tracked.txt').write_text('child')
                yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',FakeChild):
            events=[ev async for ev in self.parent._run_subagent('call','task',None,'worktree')]
        result=next(e for e in events if e['type']=='subagent_output')
        self.assertTrue(result['ok'],result)
        self.assertEqual((destinations[0]/'tracked.txt').read_text(),'child')
        self.assertEqual((self.parent.workspace/'tracked.txt').read_text(),'parent')
        self.assertNotEqual(destinations[0].resolve(),self.parent.workspace.resolve())

    @unittest.skipUnless(shutil.which('git'),'Git required')
    async def test_workspace_keyed_checkpoint_does_not_currently_satisfy_session_keyed_worktree(self):
        from engine import checkpoints
        (self.parent.workspace/'tracked.txt').write_text('parent')
        self.assertNotEqual(checkpoints.key_for(self.parent.workspace),self.parent.session.id)
        snap=await checkpoints.snapshot(checkpoints.key_for(self.parent.workspace),self.parent.workspace,'test')
        self.assertIsNotNone(snap)
        with patch.object(mod,'Agent',side_effect=AssertionError('child must not start')):
            events=[ev async for ev in self.parent._run_subagent('call','task',None,'worktree')]
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_successful_factory_binds_child_to_supplied_worktree_path(self):
        wt=self.workspaces/'mock-worktree'; wt.mkdir()
        events,paths=await self.run_child(str(wt))
        self.assertEqual(paths,[wt.resolve()]); self.assertTrue((wt/'child-marker').exists())
        self.assertFalse((self.parent.workspace/'child-marker').exists())
        self.assertEqual(sum(e['type']=='subagent_started' for e in events),1)

    async def test_unavailable_worktree_fails_without_starting_child(self):
        events,paths=await self.run_child(None)
        self.assertEqual(paths,[]); self.assertFalse((self.parent.workspace/'child-marker').exists())
        self.assertFalse(any(e['type']=='subagent_started' for e in events))
        result=next(e for e in events if e['type']=='subagent_output')
        self.assertFalse(result['ok']); self.assertIn('worktree',result['output'].lower())

    async def test_parent_directory_cannot_be_used_as_isolated_worktree(self):
        events,paths=await self.run_child(str(self.parent.workspace))
        self.assertEqual(paths,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_worktree_nested_inside_parent_is_rejected(self):
        wt=self.parent.workspace/'nested'; wt.mkdir()
        events,paths=await self.run_child(str(wt))
        self.assertEqual(paths,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_worktree_containing_parent_is_rejected(self):
        events,paths=await self.run_child(str(self.workspaces))
        self.assertEqual(paths,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_nonexistent_worktree_fails_without_starting_child(self):
        events,paths=await self.run_child(str(self.workspaces/'missing'))
        self.assertEqual(paths,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])

    async def test_worktree_pointer_write_failure_fails_without_starting_child(self):
        wt=self.workspaces/'mock-worktree'; wt.mkdir()
        original=Path.write_text
        def fail_pointer(path,*args,**kwargs):
            if path.suffix == '.path': raise OSError('private pointer failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail_pointer):
            events,paths=await self.run_child(str(wt))
        self.assertEqual(paths,[]); result=next(e for e in events if e['type']=='subagent_output')
        self.assertFalse(result['ok']); self.assertNotIn('private pointer failure',result['output'])
        self.assertFalse((wt/'child-marker').exists())

    async def test_trust_denial_from_worktree_factory_is_not_swallowed(self):
        from engine.hooks import HookTrustDenied
        with patch.object(worktree,'make',new_callable=AsyncMock,side_effect=HookTrustDenied()), \
             patch.object(mod,'Agent',side_effect=AssertionError('child must not start')):
            with self.assertRaises(HookTrustDenied):
                [ev async for ev in self.parent._run_subagent('call','task',None,'worktree')]

    async def test_worktree_factory_exception_is_reported_without_child(self):
        with patch.object(worktree,'make',new_callable=AsyncMock,side_effect=OSError('private error')), \
             patch.object(mod,'Agent',side_effect=AssertionError('child must not start')):
            events=[ev async for ev in self.parent._run_subagent('call','task',None,'worktree')]
        result=next(e for e in events if e['type']=='subagent_output')
        self.assertFalse(result['ok']); self.assertNotIn('private error',result['output'])

    async def test_worktree_must_match_resolved_child_workspace(self):
        wt=self.workspaces/'mock-worktree'; wt.mkdir()
        real=self.store.workspace
        def mismatch(sid):
            if sid != self.parent.session.id: return self.parent.workspace
            return real(sid)
        with patch.object(self.store,'workspace',side_effect=mismatch):
            events,paths=await self.run_child(str(wt))
        self.assertEqual(paths,[])
        self.assertFalse(next(e for e in events if e['type']=='subagent_output')['ok'])
