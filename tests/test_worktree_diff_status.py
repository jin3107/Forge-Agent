"""A failed worktree diff must not be reported as an empty diff."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import agent as mod, checkpoints, config, worktree
from engine.session import SessionStore, ScopedSessionStore

@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeDiffStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); s=self.store.create(model='test'); self.agent=mod.Agent(self.store,s.id)
        (self.agent.workspace/'file').write_text('base\n'); self.assertIsNotNone(await checkpoints.snapshot(s.id,self.agent.workspace,'base'))
        self.child=self.store.create(model='test'); self.wt=Path(await worktree.make(s.id,self.child.id)); self.sid=s.id

    async def test_clean_and_changed_worktrees_are_distinct_successes(self):
        result=await worktree.diff_stat_result(self.child.id,self.sid)
        self.assertTrue(result['ok']); self.assertEqual(result['stat'],'')
        (self.wt/'file').write_text('changed\n')
        result=await worktree.diff_stat_result(self.child.id,self.sid)
        self.assertTrue(result['ok']); self.assertIn('file',result['stat'])

    async def test_invalid_binding_and_git_failure_are_errors_not_empty_success(self):
        (self.wt/'.git').write_text('gitdir: /nonexistent\n')
        result=await worktree.diff_stat_result(self.child.id,self.sid)
        self.assertFalse(result['ok']); self.assertEqual(result['stat'],'')
        self.assertEqual(await worktree.diff_stat(self.child.id,self.sid),'')

    async def test_git_stage_and_diff_exceptions_are_sanitized(self):
        orig=worktree._run
        for verb in ('add','diff'):
            async def run(*args,**kwargs):
                if verb in args: raise OSError('private error')
                return await orig(*args,**kwargs)
            with self.subTest(verb=verb),patch.object(worktree,'_run',side_effect=run):
                result=await worktree.diff_stat_result(self.child.id,self.sid)
            self.assertFalse(result['ok']); self.assertNotIn('private error',str(result))

    async def test_list_reports_unavailable_for_corrupt_git_metadata(self):
        (self.wt/'.git').write_text('gitdir: /nonexistent\n')
        rows=await worktree.list_all(); row=next(r for r in rows if r['session']==self.child.id)
        self.assertFalse(row['diff_ok']); self.assertNotIn('không khác',row['stat'])
        self.assertIn('không thể',row['stat'].lower())

    async def test_child_completion_warns_when_postrun_diff_cannot_be_verified(self):
        class FakeChild:
            def __init__(self,store,sid,**kwargs): self.workspace=store.workspace(sid)
            async def send(self,task):
                (self.workspace/'file').write_text('child ran\n')
                (self.workspace/'.git').write_text('gitdir: /nonexistent\n')
                yield {'type':'assistant_message','content':'child completed'}
        with patch.object(mod,'Agent',FakeChild):
            events=[event async for event in self.agent._run_subagent('call','task',None,'worktree')]
        result=next(event for event in events if event['type']=='subagent_output')
        self.assertTrue(result['ok']); self.assertIn('child completed',result['output'])
        self.assertIn('không thể',result['output'].lower()); self.assertNotIn('không khác gì điểm',result['output'])
