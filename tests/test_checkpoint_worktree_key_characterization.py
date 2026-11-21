"""Characterize real write/checkpoint/worktree/fork semantics before key migration.

These tests describe current behavior, not a proposed security or freshness guarantee.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, tools, worktree
from engine.session import SessionStore, ScopedSessionStore


@unittest.skipUnless(shutil.which('git'), 'Git required')
class CheckpointWorktreeKeyCharacterization(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); sessions=root/'sessions'; self.wss=root/'workspaces'
        sessions.mkdir(); self.wss.mkdir()
        cp=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=self.wss,CONFIG_FILE=root/'config.json')
        cp.start(); self.addCleanup(cp.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner')
        self.session=self.store.create(model='test'); self.ws=self.store.workspace(self.session.id)
        self.ws.mkdir(exist_ok=True); (self.ws/'file.txt').write_text('before\n')

    async def write(self, content):
        with patch.object(config,'load_config',return_value={'checkpoints_enabled':True}), \
             patch('engine.lsp.available_for',return_value=False):
            return await tools.execute_tool('write_file',{'path':'file.txt','content':content},self.ws,owner_id='owner',session_id=self.session.id)

    async def test_real_tool_checkpoint_is_pre_write_and_session_worktree_cannot_use_it(self):
        result=await self.write('after\n'); self.assertTrue(result['ok'],result)
        key=checkpoints.key_for(self.ws)
        self.assertNotEqual(key,self.session.id)
        self.assertTrue(await worktree.available_for(key)); self.assertFalse(await worktree.available_for(self.session.id))
        self.assertIsNone(await worktree.make(self.session.id,'child'))
        wt=await worktree.make(key,'child'); self.assertIsNotNone(wt)
        self.assertEqual((Path(wt)/'file.txt').read_text(),'before\n')
        self.assertEqual((self.ws/'file.txt').read_text(),'after\n')
        result=await worktree.discard('child',key); self.assertTrue(result['ok'],result)

    async def test_fork_without_session_checkpoint_copies_current_workspace(self):
        result=await self.write('after\n'); self.assertTrue(result['ok'],result)
        fork=self.store.fork(self.session.id); self.assertIsNotNone(fork)
        self.assertEqual((self.store.workspace(fork.id)/'file.txt').read_text(),'after\n')

    async def test_fork_with_old_session_checkpoint_uses_session_snapshot_not_current(self):
        snap=await checkpoints.snapshot(self.session.id,self.ws,'legacy'); self.assertIsNotNone(snap)
        result=await self.write('after\n'); self.assertTrue(result['ok'],result)
        fork=self.store.fork(self.session.id); self.assertIsNotNone(fork)
        self.assertEqual((self.store.workspace(fork.id)/'file.txt').read_text(),'before\n')
        self.assertEqual((self.ws/'file.txt').read_text(),'after\n')

    async def test_path_aliases_of_same_directory_have_different_checkpoint_keys(self):
        alias=self.wss/'alias'
        try:
            alias.symlink_to(self.ws, target_is_directory=True)
        except (OSError,NotImplementedError):
            self.skipTest('symlink unavailable')
        self.assertEqual(alias.resolve(),self.ws.resolve())
        self.assertNotEqual(checkpoints.key_for(alias),checkpoints.key_for(self.ws))
        self.assertTrue((await self.write('after\n'))['ok'])
        self.assertTrue(await worktree.available_for(checkpoints.key_for(self.ws)))
        self.assertFalse(await worktree.available_for(checkpoints.key_for(alias)))

    async def test_distinct_sessions_shared_workspace_use_same_checkpoint_key(self):
        other=self.store.create(model='test')
        (self.wss/f'{other.id}.path').write_text(str(self.ws))
        other_ws=self.store.workspace(other.id)
        self.assertEqual(other_ws,self.ws)
        self.assertEqual(checkpoints.key_for(other_ws),checkpoints.key_for(self.ws))
        self.assertNotEqual(other.id,self.session.id)
        result=await self.write('after\n'); self.assertTrue(result['ok'],result)
        self.assertTrue(await worktree.available_for(checkpoints.key_for(other_ws)))
        self.assertFalse(await worktree.available_for(other.id))
