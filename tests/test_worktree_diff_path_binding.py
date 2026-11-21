"""Worktree diff must not stage a different directory using copied Git metadata."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class DiffPathBinding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.parent=self.wss/'parent'; self.parent.mkdir(); (self.parent/'file.txt').write_text('base\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.parent,'base'))
        self.wt=Path(await worktree.make('parent','child'))
        (self.wt/'file.txt').write_text('actual child\n')

    def index(self):
        result=subprocess.run([shutil.which('git'),'-C',str(self.wt),'ls-files','--stage'],check=True,capture_output=True)
        return result.stdout

    async def test_copied_git_pointer_cannot_stage_from_another_directory(self):
        fake=self.wss/'impostor'; fake.mkdir(); (fake/'.git').write_bytes((self.wt/'.git').read_bytes())
        (fake/'file.txt').write_text('impostor content\n'); (fake/'foreign.txt').write_text('foreign\n')
        # Git sees the same repo/branch, but the reciprocal worktree backlink rejects it.
        self.assertFalse(await worktree._worktree_matches(fake,worktree._repo('parent'),'child'))
        before=self.index()
        self.assertEqual(await worktree.diff_stat('child','parent',fake),'')
        self.assertEqual(self.index(),before)
        self.assertEqual((self.wt/'file.txt').read_text(),'actual child\n')

    async def test_expected_path_and_default_path_still_work(self):
        self.assertIn('file.txt',await worktree.diff_stat('child','parent',self.wt))
        self.assertIn('file.txt',await worktree.diff_stat('child','parent'))

    async def test_missing_or_unrelated_path_fails_without_git(self):
        other=self.wss/'other'; other.mkdir()
        for candidate in (other,self.wss/'missing'):
            with self.subTest(path=candidate),patch.object(worktree,'_run') as run:
                self.assertEqual(await worktree.diff_stat('child','parent',candidate),'')
                run.assert_not_called()

    async def test_merge_rejects_symlink_replacing_expected_worktree_path(self):
        other=self.wss/'other'; self.wt.rename(other)
        try: self.wt.symlink_to(other,target_is_directory=True)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        with patch.object(worktree,'_run') as run:
            result=await worktree.merge('child','parent','parent',self.parent)
            self.assertFalse(result['ok'],result); run.assert_not_called()
        self.assertEqual((self.parent/'file.txt').read_text(),'base\n')

    async def test_symlink_replacing_expected_target_is_rejected(self):
        other=self.wss/'other'; self.wt.rename(other)
        try: self.wt.symlink_to(other,target_is_directory=True)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        with patch.object(worktree,'_run') as run:
            self.assertEqual(await worktree.diff_stat('child','parent'), '')
            run.assert_not_called()
        self.assertEqual((other/'file.txt').read_text(),'actual child\n')
