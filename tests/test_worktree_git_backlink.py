"""Real Git linked-worktree forward/backlink consistency, not an attestation."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints,config,worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class GitBacklink(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.parent=self.wss/'parent'; self.parent.mkdir(); (self.parent/'file').write_text('original\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.parent,'base'))
        self.wt=Path(await worktree.make('parent','child')); (self.wt/'file').write_text('child\n')
        rc,admin=await worktree._run('-C',str(self.wt),'rev-parse','--git-dir'); self.assertEqual(rc,0)
        self.admin=Path(admin) if Path(admin).is_absolute() else self.wt/admin
        self.repo=worktree._repo('parent')

    async def test_genuine_git_worktree_still_matches(self):
        self.assertTrue(await worktree._worktree_matches(self.wt,self.repo,'child'))
        self.assertIn('file',await worktree.diff_stat('child','parent'))

    async def test_relative_backlink_is_resolved_from_git_admin_directory(self):
        import os
        (self.admin/'gitdir').write_text(os.path.relpath(self.wt/'.git',self.admin)+'\n')
        self.assertTrue(await worktree._worktree_matches(self.wt,self.repo,'child'))

    async def test_copied_git_pointer_at_other_path_fails_binding(self):
        fake=self.wss/'fake'; fake.mkdir(); (fake/'.git').write_bytes((self.wt/'.git').read_bytes()); (fake/'file').write_text('fake')
        self.assertFalse(await worktree._worktree_matches(fake,self.repo,'child'))
        self.assertEqual((self.wt/'file').read_text(),'child\n')

    async def test_replacement_at_same_path_can_still_pass_backlink_checks(self):
        # The backlink identifies a path, not its original directory inode.
        moved=self.wss/'moved-original'; self.wt.rename(moved); self.wt.mkdir()
        (self.wt/'.git').write_bytes((moved/'.git').read_bytes()); (self.wt/'file').write_text('replacement\n')
        self.assertTrue(await worktree._worktree_matches(self.wt,self.repo,'child'))
        self.assertEqual((moved/'file').read_text(),'child\n')

    async def test_tampered_backlink_rejects_diff_merge_and_discard(self):
        (self.admin/'gitdir').write_text(str(self.wss/'wrong'/'.git')+'\n')
        self.assertFalse(await worktree._worktree_matches(self.wt,self.repo,'child'))
        self.assertEqual(await worktree.diff_stat('child','parent'),'')
        self.assertFalse((await worktree.merge('child','parent','parent',self.parent))['ok'])
        self.assertFalse((await worktree.discard('child','parent'))['ok'])
        self.assertEqual((self.parent/'file').read_text(),'original\n'); self.assertEqual((self.wt/'file').read_text(),'child\n')

    async def test_missing_or_symlink_backlink_rejected(self):
        back=self.admin/'gitdir'; saved=back.read_text(); back.unlink()
        self.assertFalse(await worktree._worktree_matches(self.wt,self.repo,'child'))
        actual=self.wss/'backlink-target'; actual.write_text(saved)
        try: back.symlink_to(actual)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        self.assertFalse(await worktree._worktree_matches(self.wt,self.repo,'child'))

    async def test_oversized_backlink_rejected(self):
        (self.admin/'gitdir').write_bytes(b'x'*8193)
        self.assertFalse(await worktree._worktree_matches(self.wt,self.repo,'child'))
