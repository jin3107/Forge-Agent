"""Test-only binding validator; it does not authenticate its caller's record."""
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from engine import config, worktree, worktree_base
from engine.session import SessionStore, ScopedSessionStore

@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeBindingPrototype(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.base=SessionStore(); self.store=ScopedSessionStore(self.base,'owner'); self.other=ScopedSessionStore(self.base,'other')
        self.parent=self.store.create(model='test'); self.child=self.store.create(model='test')
        self.ws=self.store.workspace(self.parent.id); (self.ws/'file').write_text('base')
        captured=await worktree_base.capture(self.child.id,self.ws); self.assertTrue(captured['ok'],captured)
        self.key=captured['repo_key']; self.commit=captured['commit']
        self.wt=Path(await worktree.make(self.key,self.child.id))
        (self.wss/f'{self.child.id}.path').write_text(str(self.wt))

    def binding(self):
        from engine.worktree_binding import Binding
        return Binding(owner_id='owner',parent_sid=self.parent.id,child_sid=self.child.id,
                       parent_workspace=str(self.ws.resolve()),repo_key=self.key,base_commit=self.commit)

    async def validate(self,binding=None,store=None):
        from engine.worktree_binding import validate
        return await validate(store or self.store,binding or self.binding())

    async def test_valid_binding_survives_reopening_scoped_store_and_child_edits(self):
        self.assertTrue(await self.validate())
        (self.wt/'file').write_text('child edit')
        reopened=ScopedSessionStore(SessionStore(),'owner')
        self.assertTrue(await self.validate(store=reopened))

    async def test_persisted_record_scoped_lookup_then_live_revalidation(self):
        from engine.worktree_binding_registry import BindingRegistry
        from engine.worktree_binding import validate
        db=self.wss.parent/'binding-test.sqlite3'; registry=BindingRegistry(db)
        self.assertEqual(registry.record(self.binding()),'created')
        another=self.store.create(model='test')
        (self.wss/f'{another.id}.path').write_text(str(self.ws))
        reopened=BindingRegistry(db)
        self.assertEqual(reopened.lookup('owner',another.id,self.child.id),('missing',None))
        status,record=reopened.lookup('owner',self.parent.id,self.child.id)
        self.assertEqual(status,'found'); self.assertTrue(await validate(self.store,record))
        (self.wss/f'{self.child.id}.path').unlink()
        self.assertFalse(await validate(self.store,record))

    async def test_forged_record_for_other_owned_parent_sharing_workspace_can_pass(self):
        # The validator cannot establish who issued a record or the original parent.
        another=self.store.create(model='test')
        (self.wss/f'{another.id}.path').write_text(str(self.ws))
        forged=replace(self.binding(),parent_sid=another.id)
        self.assertTrue(await self.validate(forged))

    async def test_wrong_owner_wrong_parent_or_missing_sessions_rejected(self):
        self.assertFalse(await self.validate(store=self.other))
        stranger=self.other.create(model='test')
        self.assertFalse(await self.validate(replace(self.binding(),parent_sid=stranger.id)))
        self.assertFalse(await self.validate(replace(self.binding(),owner_id='other')))
        self.store.delete(self.parent.id); self.assertFalse(await self.validate())

    async def test_changed_parent_or_child_pointer_is_rejected(self):
        alternate=self.wss/'alternate'; alternate.mkdir()
        (self.wss/f'{self.parent.id}.path').write_text(str(alternate))
        self.assertFalse(await self.validate())
        (self.wss/f'{self.parent.id}.path').unlink()
        (self.wss/f'{self.child.id}.path').write_text(str(alternate))
        self.assertFalse(await self.validate())

    async def test_key_commit_and_workspace_mismatch_rejected(self):
        b=self.binding()
        for altered in (replace(b,repo_key='different'),replace(b,base_commit='0'*40),
                        replace(b,parent_workspace=str(self.wss)),replace(b,child_sid='../escape')):
            self.assertFalse(await self.validate(altered),altered)

    async def test_detached_head_or_modified_git_pointer_rejected(self):
        rc,_=await worktree._run('-C',str(self.wt),'checkout','--detach'); self.assertEqual(rc,0)
        self.assertFalse(await self.validate())
        rc,_=await worktree._run('-C',str(self.wt),'checkout',f'iso/{self.child.id}'); self.assertEqual(rc,0)
        (self.wt/'.git').write_text('gitdir: /nonexistent\n')
        self.assertFalse(await self.validate())

    async def test_changed_forge_ref_rejected(self):
        repo=worktree._repo(self.key)
        rc,_=await worktree._run('--git-dir',str(repo),'update-ref','-d','refs/heads/forge'); self.assertEqual(rc,0)
        self.assertFalse(await self.validate())

    async def test_changed_child_branch_commit_rejected(self):
        rc,_=await worktree._run('-C',str(self.wt),'commit','--allow-empty','-m','changed branch'); self.assertEqual(rc,0)
        self.assertFalse(await self.validate())

    async def test_unscoped_store_and_malformed_input_rejected(self):
        from engine.worktree_binding import validate
        self.assertFalse(await validate(self.base,self.binding()))
        self.assertFalse(await validate(self.store,{'owner_id':'owner'}))
