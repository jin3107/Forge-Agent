"""Real-Git worktree merge must checkpoint the parent before applying changes."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config, worktree


@unittest.skipUnless(shutil.which('git'), 'Git required')
class MergeCheckpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); wsdir=root/'workspaces'; wsdir.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        patcher=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=wsdir,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json')
        patcher.start(); self.addCleanup(patcher.stop)
        self.ws=wsdir/'parent'; self.ws.mkdir(); (self.ws/'file.txt').write_text('original\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent-id',self.ws,'worktree base'))
        self.wt=Path(await worktree.make('parent-id','child-id'))
        (self.wt/'file.txt').write_text('merged\n')

    async def test_successful_merge_then_workspace_key_undo_restores_pre_merge(self):
        result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result); self.assertEqual((self.ws/'file.txt').read_text(),'merged\n')
        undo=await checkpoints.undo(checkpoints.key_for(self.ws),self.ws)
        self.assertTrue(undo['ok'],undo); self.assertEqual((self.ws/'file.txt').read_text(),'original\n')

    async def test_checkpoint_failure_does_not_hide_merged_change(self):
        with patch.object(checkpoints,'snapshot',new_callable=AsyncMock,return_value=None):
            result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result); self.assertIn('checkpoint',result['message'].lower())
        self.assertEqual((self.ws/'file.txt').read_text(),'merged\n')

    async def test_disabled_checkpoints_warn_while_merge_still_succeeds(self):
        with patch.object(config,'load_config',return_value={'checkpoints_enabled':False}):
            result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result); self.assertFalse(result['checkpoint_created'])
        self.assertIn('checkpoint',result['message'].lower())
        self.assertEqual((self.ws/'file.txt').read_text(),'merged\n')

    async def test_existing_undo_stack_keeps_previous_entry_after_merge_undo(self):
        key=checkpoints.key_for(self.ws)
        (self.ws/'file.txt').write_text('earlier\n')
        self.assertIsNotNone(await checkpoints.snapshot(key,self.ws,'earlier state'))
        (self.ws/'file.txt').write_text('original\n')
        result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result)
        self.assertTrue((await checkpoints.undo(key,self.ws))['ok'])
        self.assertEqual((self.ws/'file.txt').read_text(),'original\n')
        self.assertTrue((await checkpoints.undo(key,self.ws))['ok'])
        self.assertEqual((self.ws/'file.txt').read_text(),'earlier\n')

    async def test_checkpoint_exception_is_reported_without_private_details(self):
        with patch.object(checkpoints,'snapshot',new_callable=AsyncMock,side_effect=OSError('private path')):
            result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result); self.assertIn('checkpoint',result['message'].lower())
        self.assertNotIn('private path',result['message']); self.assertEqual((self.ws/'file.txt').read_text(),'merged\n')

    async def test_empty_diff_does_not_create_undo_checkpoint(self):
        (self.wt/'file.txt').write_text('original\n')
        with patch.object(checkpoints,'snapshot',new_callable=AsyncMock) as snapshot:
            result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertTrue(result['ok'],result); snapshot.assert_not_awaited()

    async def test_apply_failure_retains_pre_merge_checkpoint_and_reports_failure(self):
        (self.ws/'file.txt').write_text('conflicting change\n')
        result=await worktree.merge('child-id','parent-id','parent-id',self.ws)
        self.assertFalse(result['ok'],result); self.assertEqual((self.ws/'file.txt').read_text(),'conflicting change\n')
        self.assertTrue(await worktree.available_for(checkpoints.key_for(self.ws)))
