"""Worktree creation must not overwrite preexisting targets or dirty worktrees."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config, worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class MakeCollision(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=self.wss/'parent'; self.ws.mkdir(); (self.ws/'file.txt').write_text('parent\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.ws,'base'))

    async def test_repeated_make_preserves_existing_dirty_worktree(self):
        path=await worktree.make('parent','child'); self.assertIsNotNone(path); wt=Path(path)
        (wt/'file.txt').write_text('unmerged change\n'); (wt/'new.txt').write_text('new data\n')
        self.assertIsNone(await worktree.make('parent','child'))
        self.assertTrue(wt.exists()); self.assertEqual((wt/'file.txt').read_text(),'unmerged change\n')
        self.assertEqual((wt/'new.txt').read_text(),'new data\n')
        self.assertTrue(await worktree._worktree_matches(wt,worktree._repo('parent'),'child'))

    async def test_preexisting_unrelated_target_is_not_removed_or_touched(self):
        target=worktree.wt_path('child'); target.mkdir(); (target/'keep.txt').write_text('keep')
        result=await worktree.make('parent','child')
        self.assertIsNone(result); self.assertEqual((target/'keep.txt').read_text(),'keep')
        rc,_=await worktree._run('--git-dir',str(worktree._repo('parent')),'rev-parse','--verify','refs/heads/iso/child')
        self.assertNotEqual(rc,0)

    async def test_dangling_symlink_target_is_not_followed_or_replaced(self):
        target=worktree.wt_path('child'); missing=self.wss/'missing'
        try: target.symlink_to(missing,target_is_directory=True)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        with patch.object(worktree,'_run',new_callable=AsyncMock) as run:
            self.assertIsNone(await worktree.make('parent','child'))
            run.assert_not_awaited()
        self.assertTrue(target.is_symlink()); self.assertFalse(missing.exists())

    async def test_existing_empty_target_is_not_reused(self):
        target=worktree.wt_path('child'); target.mkdir()
        with patch.object(worktree,'_run',new_callable=AsyncMock) as run:
            self.assertIsNone(await worktree.make('parent','child'))
            run.assert_not_awaited()
        self.assertTrue(target.is_dir()); self.assertEqual(list(target.iterdir()),[])

    async def test_target_appearing_during_availability_check_is_not_used(self):
        target=worktree.wt_path('child')
        async def available(sid):
            target.mkdir(); (target/'keep').write_text('keep'); return True
        with patch.object(worktree,'available_for',side_effect=available), \
             patch.object(worktree,'_run',new_callable=AsyncMock) as run:
            self.assertIsNone(await worktree.make('parent','child'))
            run.assert_not_awaited()
        self.assertEqual((target/'keep').read_text(),'keep')

    async def test_fresh_target_still_creates_worktree_and_branch(self):
        path=await worktree.make('parent','fresh'); self.assertIsNotNone(path)
        wt=Path(path); self.assertEqual((wt/'file.txt').read_text(),'parent\n')
        self.assertTrue(await worktree._worktree_matches(wt,worktree._repo('parent'),'fresh'))
