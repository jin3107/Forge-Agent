"""Direct checkpoint APIs reject unsafe keys before constructing paths or spawning Git."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config

class CheckpointIDBoundary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=self.wss/'parent'; self.ws.mkdir(); (self.ws/'file').write_text('data')
        self.bad=['../child','a/../child','bad.id','','x'*300,'a\x00b',None,123]

    async def test_path_helpers_reject_bad_keys(self):
        for key in self.bad:
            with self.subTest(key=key):
                with self.assertRaises(ValueError): checkpoints._repo_dir(key)
                with self.assertRaises(ValueError): checkpoints._git_args(key,self.ws)
        self.assertEqual(checkpoints._repo_dir('session-1'),self.wss/'.ckpt-session-1.git')
        self.assertEqual(checkpoints._repo_dir('wtbase-child'),self.wss/'.ckpt-wtbase-child.git')
        self.assertEqual(checkpoints._repo_dir(checkpoints.key_for(self.ws)).parent,self.wss)

    async def test_invalid_calls_fail_without_git_or_repo_creation(self):
        before=set(self.wss.iterdir())
        with patch.object(checkpoints,'GIT','git'),patch.object(checkpoints,'_run_with_env',new_callable=AsyncMock) as run:
            for key in self.bad:
                with self.subTest(key=key):
                    outcome={}
                    self.assertIsNone(await checkpoints.snapshot(key,self.ws,'test',outcome=outcome))
                    self.assertEqual(outcome.get('status'),'failed')
                    self.assertFalse((await checkpoints.undo(key,self.ws))['ok'])
                    self.assertEqual(await checkpoints.list_checkpoints(key,self.ws),[])
            run.assert_not_awaited()
        self.assertEqual(set(self.wss.iterdir()),before)

    @unittest.skipUnless(shutil.which('git'),'Git required')
    async def test_valid_workspace_key_still_supports_snapshot_and_undo(self):
        key=checkpoints.key_for(self.ws)
        self.assertIsNotNone(await checkpoints.snapshot(key,self.ws,'base'))
        (self.ws/'file').write_text('changed')
        self.assertTrue((await checkpoints.undo(key,self.ws))['ok'])
        self.assertEqual((self.ws/'file').read_text(),'data')
