"""Real Git worktree operations must not alter the checkpoint repo's main index."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree


@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeIndexIntegrity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); sessions=root/'sessions'; workspaces=root/'workspaces'
        sessions.mkdir(); workspaces.mkdir()
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=workspaces,CONFIG_FILE=root/'config.json')
        cp.start(); self.addCleanup(cp.stop)
        self.parent=workspaces/'parent'; self.parent.mkdir()
        (self.parent/'tracked.txt').write_text('parent original\n')
        self.sid='parent-session'; self.sub='child-session'
        outcome={}
        snap=await checkpoints.snapshot(self.sid,self.parent,'initial',outcome=outcome)
        self.assertIsNotNone(snap,outcome)
        path=await worktree.make(self.sid,self.sub)
        self.assertIsNotNone(path); self.wt=Path(path)
        self.repo=workspaces/f'.ckpt-{self.sid}.git'
        (self.wt/'tracked.txt').write_text('child changed\n')
        (self.wt/'new.txt').write_text('new child file\n')

    def index(self):
        r=subprocess.run([shutil.which('git'),'--git-dir',str(self.repo),'ls-files','--stage'],
                         check=True,capture_output=True)
        return r.stdout

    async def test_diff_stat_does_not_change_checkpoint_main_index(self):
        before=self.index()
        stat=await worktree.diff_stat(self.sub,self.sid,self.wt)
        self.assertIn('tracked.txt',stat); self.assertIn('new.txt',stat)
        self.assertEqual(self.index(),before,'worktree diff must use the worktree index, not parent checkpoint index')
        self.assertEqual((self.parent/'tracked.txt').read_text(),'parent original\n')
        self.assertFalse((self.parent/'new.txt').exists())

    async def test_inherited_git_index_override_does_not_touch_parent_index(self):
        before=self.index(); parent_index=self.repo/'index'
        with patch.dict('os.environ',{'GIT_INDEX_FILE':str(parent_index)}):
            stat=await worktree.diff_stat(self.sub,self.sid,self.wt)
            self.assertIn('tracked.txt',stat)
            result=await worktree.merge(self.sub,self.sid,self.sid,self.parent)
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.index(),before)
        self.assertEqual((self.parent/'tracked.txt').read_text(),'child changed\n')

    async def test_detached_worktree_is_rejected_before_staging_or_merge(self):
        subprocess.run([shutil.which('git'),'-C',str(self.wt),'checkout','--detach'],check=True,capture_output=True)
        before=self.index()
        self.assertEqual(await worktree.diff_stat(self.sub,self.sid,self.wt),'')
        result=await worktree.merge(self.sub,self.sid,self.sid,self.parent)
        self.assertFalse(result['ok']); self.assertIn('không khớp',result['message'])
        self.assertEqual(self.index(),before)
        self.assertEqual((self.parent/'tracked.txt').read_text(),'parent original\n')
        self.assertFalse((self.parent/'new.txt').exists())

    async def test_modified_git_pointer_is_rejected(self):
        before=self.index(); (self.wt/'.git').write_text('gitdir: /nonexistent/test-git-dir\n')
        self.assertEqual(await worktree.diff_stat(self.sub,self.sid,self.wt),'')
        result=await worktree.merge(self.sub,self.sid,self.sid,self.parent)
        self.assertFalse(result['ok']); self.assertEqual(self.index(),before)
        self.assertEqual((self.parent/'tracked.txt').read_text(),'parent original\n')

    async def test_failed_worktree_stage_does_not_apply_patch(self):
        original=worktree._run
        async def broken(*args,**kwargs):
            if 'add' in args: return 1,'private error'
            return await original(*args,**kwargs)
        before=self.index()
        with patch.object(worktree,'_run',side_effect=broken):
            self.assertEqual(await worktree.diff_stat(self.sub,self.sid,self.wt),'')
            result=await worktree.merge(self.sub,self.sid,self.sid,self.parent)
        self.assertFalse(result['ok']); self.assertNotIn('private error',result['message'])
        self.assertEqual(self.index(),before)
        self.assertEqual((self.parent/'tracked.txt').read_text(),'parent original\n')
        self.assertFalse((self.parent/'new.txt').exists())

    async def test_merge_does_not_change_checkpoint_main_index(self):
        before=self.index()
        result=await worktree.merge(self.sub,self.sid,self.sid,self.parent)
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.index(),before,'worktree merge must not stage into the parent checkpoint index')
        self.assertEqual((self.parent/'tracked.txt').read_text(),'child changed\n')
        self.assertEqual((self.parent/'new.txt').read_text(),'new child file\n')
