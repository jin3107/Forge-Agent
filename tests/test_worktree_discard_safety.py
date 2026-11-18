"""Real-Git discard must not delete unrelated checkpoints or report false success."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree
from _win_compat import overwrite_file

@unittest.skipUnless(shutil.which('git'), 'Git required')
class DiscardSafety(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=self.wss,CONFIG_FILE=root/'config.json'); cp.start(); self.addCleanup(cp.stop)
        self.parent=self.wss/'parent-ws'; self.parent.mkdir(); (self.parent/'x').write_text('parent')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.parent,'parent'))
        self.wt=Path(await worktree.make('parent','child')); (self.wt/'x').write_text('child')
        self.other=self.wss/'other-ws'; self.other.mkdir(); (self.other/'x').write_text('other')
        self.assertIsNotNone(await checkpoints.snapshot('other',self.other,'other'))
        self.assertIsNotNone(await checkpoints.snapshot('child',self.other,'child-owned-history'))
        self.child_repo=self.wss/'.ckpt-child.git'

    async def test_wrong_parent_does_not_remove_anything_or_claim_success(self):
        result=await worktree.discard('child','other')
        self.assertFalse(result['ok'],result); self.assertEqual(result['removed'],[])
        self.assertEqual((self.wt/'x').read_text(),'child'); self.assertTrue(self.child_repo.exists())
        self.assertTrue(await worktree._worktree_matches(self.wt,worktree._repo('parent'),'child'))

    async def test_valid_discard_preserves_child_checkpoint_repo(self):
        result=await worktree.discard('child','parent')
        self.assertTrue(result['ok'],result); self.assertFalse(self.wt.exists())
        self.assertTrue(self.child_repo.exists())
        self.assertEqual((self.parent/'x').read_text(),'parent')

    async def test_failed_remove_is_not_reported_success_or_followed_by_branch_delete(self):
        original=worktree._run; calls=[]
        async def run(*args,**kwargs):
            calls.append(args)
            if 'remove' in args: return 1,'private failure'
            return await original(*args,**kwargs)
        with patch.object(worktree,'_run',side_effect=run):
            result=await worktree.discard('child','parent')
        self.assertFalse(result['ok'],result); self.assertEqual(result['removed'],[])
        self.assertTrue(self.wt.exists()); self.assertTrue(self.child_repo.exists())
        self.assertFalse(any('branch' in c or 'prune' in c for c in calls))

    async def test_branch_failure_reports_partial_removal(self):
        original=worktree._run
        async def run(*args,**kwargs):
            if 'branch' in args: return 1, 'private failure'
            return await original(*args,**kwargs)
        with patch.object(worktree,'_run',side_effect=run):
            result=await worktree.discard('child','parent')
        self.assertFalse(result['ok']); self.assertEqual(result['removed'],[str(self.wt)])
        self.assertIn('branch',result['message']); self.assertNotIn('private failure',result['message'])
        self.assertFalse(self.wt.exists()); self.assertTrue(self.child_repo.exists())

    async def test_altered_git_pointer_is_not_deleted(self):
        overwrite_file(self.wt/'.git', 'gitdir: /nonexistent\n')
        result=await worktree.discard('child','parent')
        self.assertFalse(result['ok'],result); self.assertTrue(self.wt.exists()); self.assertTrue(self.child_repo.exists())
