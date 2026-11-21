"""Fork must not silently substitute a live workspace when its snapshot export fails."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree
from engine.session import SessionStore, ScopedSessionStore

@unittest.skipUnless(shutil.which('git'),'Git required')
class ForkFailureSemantics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); self.parent=self.store.create(model='test'); self.ws=self.store.workspace(self.parent.id)
        (self.ws/'file').write_text('snapshot\n'); self.assertIsNotNone(await checkpoints.snapshot(self.parent.id,self.ws,'base'))
        (self.ws/'file').write_text('live\n')

    async def test_oversized_snapshot_fails_rather_than_copy_live(self):
        from engine.session import ForkError
        with patch.object(worktree,'WORKTREE_ARCHIVE_LIMIT',1024):
            with self.assertRaises(ForkError) as failure: self.store.fork(self.parent.id)
        child=failure.exception.child_sid; self.assertIsNotNone(self.store.get(child))
        self.assertFalse((self.wss/child/'file').exists()); self.assertEqual((self.ws/'file').read_text(),'live\n')

    async def test_invalid_snapshot_and_export_exception_do_not_fallback(self):
        from engine.session import ForkError
        for action in (False, OSError('private details')):
            with self.subTest(action=action):
                kwargs={'return_value':False} if action is False else {'side_effect':action}
                with patch.object(worktree,'export_tree_sync',**kwargs):
                    with self.assertRaises(ForkError) as failure: self.store.fork(self.parent.id)
                self.assertNotIn('private details',str(failure.exception))
                self.assertFalse((self.wss/failure.exception.child_sid/'file').exists())

    async def test_absent_session_keyed_repo_still_copies_current_workspace(self):
        other=self.store.create(model='test'); ws=self.store.workspace(other.id); (ws/'file').write_text('live\n')
        child=self.store.fork(other.id); self.assertEqual((self.store.workspace(child.id)/'file').read_text(),'live\n')

    async def test_valid_snapshot_still_forks_snapshot(self):
        child=self.store.fork(self.parent.id); self.assertEqual((self.store.workspace(child.id)/'file').read_text(),'snapshot\n')

    async def test_dangling_checkpoint_repo_symlink_does_not_fallback(self):
        from engine.session import ForkError
        other=self.store.create(model='test'); ws=self.store.workspace(other.id); (ws/'file').write_text('live\n')
        try: worktree._repo(other.id).symlink_to(self.wss/'missing',target_is_directory=True)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        with self.assertRaises(ForkError) as failure: self.store.fork(other.id)
        self.assertFalse((self.wss/failure.exception.child_sid/'file').exists())

    async def test_partial_archive_failure_leaves_artifacts_and_does_not_copy_live_files(self):
        from engine.session import ForkError
        def partial(sid,dst):
            dst.mkdir(); (dst/'partial').write_text('partial'); return False
        with patch.object(worktree,'export_tree_sync',side_effect=partial):
            with self.assertRaises(ForkError) as failure: self.store.fork(self.parent.id)
        dst=self.wss/failure.exception.child_sid
        self.assertEqual((dst/'partial').read_text(),'partial'); self.assertFalse((dst/'file').exists())

    async def test_existing_destination_is_not_reused_or_overwritten(self):
        from engine.session import ForkError
        original=self.store.base.create
        def create(**kwargs):
            child=original(**kwargs); dst=self.wss/child.id; dst.mkdir(); (dst/'keep').write_text('keep'); return child
        with patch.object(self.store.base,'create',side_effect=create):
            with self.assertRaises(ForkError) as failure: self.store.fork(self.parent.id)
        self.assertEqual((self.wss/failure.exception.child_sid/'keep').read_text(),'keep')

    async def test_valid_workspace_pointer_remains_shared(self):
        (self.wss/f'{self.parent.id}.path').write_text(str(self.ws))
        child=self.store.fork(self.parent.id)
        self.assertEqual(self.store.workspace(child.id),self.ws)

    async def test_pointer_write_failure_does_not_silently_copy_workspace(self):
        from engine.session import ForkError
        (self.wss/f'{self.parent.id}.path').write_text(str(self.ws))
        orig=Path.open
        def fail(path,*args,**kwargs):
            if path.suffix=='.path' and args and args[0]=='x': raise OSError('private')
            return orig(path,*args,**kwargs)
        with patch.object(Path,'open',fail):
            with self.assertRaises(ForkError) as failure: self.store.fork(self.parent.id)
        self.assertNotIn('private',str(failure.exception)); self.assertFalse((self.wss/failure.exception.child_sid/'file').exists())
