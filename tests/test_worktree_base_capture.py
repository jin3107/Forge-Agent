"""Real-Git tests for the standalone, not-yet-integrated worktree base capture."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeBaseCapture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=self.wss/'parent'; self.ws.mkdir(); (self.ws/'file.txt').write_text('before\n')
        self.undo_key=checkpoints.key_for(self.ws)
        self.assertIsNotNone(await checkpoints.snapshot(self.undo_key,self.ws,'pre-write'))
        (self.ws/'file.txt').write_text('current\n')

    async def test_current_capture_does_not_change_undo_history_and_can_fork(self):
        from engine import worktree_base
        undo_repo=checkpoints._repo_dir(self.undo_key)
        _, before=await worktree._run('--git-dir',str(undo_repo),'rev-parse','refs/heads/forge')
        result=await worktree_base.capture('child-01',self.ws)
        self.assertTrue(result['ok'],result); key=result['repo_key']
        self.assertNotEqual(key,self.undo_key)
        self.assertEqual(result['commit'],(await worktree._run('--git-dir',str(checkpoints._repo_dir(key)),'rev-parse','refs/heads/forge'))[1])
        path=await worktree.make(key,'child-01'); self.assertIsNotNone(path)
        self.assertEqual((Path(path)/'file.txt').read_text(),'current\n')
        (Path(path)/'file.txt').write_text('child\n')
        self.assertEqual((self.ws/'file.txt').read_text(),'current\n')
        _, after=await worktree._run('--git-dir',str(undo_repo),'rev-parse','refs/heads/forge')
        self.assertEqual(before,after)
        self.assertTrue((await worktree.discard('child-01',key))['ok'])
        self.assertTrue((await checkpoints.undo(self.undo_key,self.ws))['ok'])
        self.assertEqual((self.ws/'file.txt').read_text(),'before\n')

    async def test_collision_does_not_touch_existing_base(self):
        from engine import worktree_base
        first=await worktree_base.capture('child-02',self.ws); self.assertTrue(first['ok'],first)
        (self.ws/'file.txt').write_text('newer\n')
        second=await worktree_base.capture('child-02',self.ws); self.assertFalse(second['ok'],second)
        self.assertNotIn('repo_key',second)
        _, ref=await worktree._run('--git-dir',str(checkpoints._repo_dir(first['repo_key'])),'rev-parse','refs/heads/forge')
        self.assertEqual(ref,first['commit'])

    async def test_invalid_child_key_or_unavailable_workspace_fails_without_repo(self):
        from engine import worktree_base
        for child in ('../escape','','a/b','a'*81):
            result=await worktree_base.capture(child,self.ws); self.assertFalse(result['ok'],result)
        result=await worktree_base.capture('child-03',self.wss/'missing'); self.assertFalse(result['ok'],result)
        self.assertFalse((self.wss/'.ckpt-wtbase-child-03.git').exists())

    async def test_git_failure_fails_closed_and_does_not_delete_partial_repo(self):
        from engine import worktree_base
        orig=checkpoints._run_with_env
        async def run(args,env,**kwargs):
            if 'write-tree' in args: return 1,'private detail'
            return await orig(args,env,**kwargs)
        with patch.object(checkpoints,'_run_with_env',side_effect=run):
            result=await worktree_base.capture('child-04',self.ws)
        self.assertFalse(result['ok']); self.assertNotIn('private detail',str(result)); self.assertNotIn('repo_key',result)
        self.assertTrue((self.wss/'.ckpt-wtbase-child-04.git').exists())
        self.assertFalse((await worktree_base.capture('child-04',self.ws))['ok'])

    async def test_concurrent_capture_same_child_reserves_only_one_repo(self):
        from engine import worktree_base
        first,second=await asyncio.gather(worktree_base.capture('child-concurrent',self.ws),
                                          worktree_base.capture('child-concurrent',self.ws))
        self.assertEqual(sum(bool(x['ok']) for x in (first,second)),1)
        success=first if first['ok'] else second
        self.assertTrue(await worktree.available_for(success['repo_key']))

    async def test_cancellation_retains_partial_repo_and_propagates(self):
        from engine import worktree_base
        started=asyncio.Event(); orig=checkpoints._run_with_env
        async def run(args,env,**kwargs):
            if 'write-tree' in args:
                started.set(); await asyncio.Event().wait()
            return await orig(args,env,**kwargs)
        with patch.object(checkpoints,'_run_with_env',side_effect=run):
            task=asyncio.create_task(worktree_base.capture('child-cancel',self.ws))
            await asyncio.wait_for(started.wait(),5); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue((self.wss/'.ckpt-wtbase-child-cancel.git').exists())
        self.assertFalse((await worktree_base.capture('child-cancel',self.ws))['ok'])

    async def test_inherited_git_index_override_does_not_touch_undo_index(self):
        from engine import worktree_base
        index=checkpoints._repo_dir(self.undo_key)/'index'; before=index.read_bytes()
        with patch.dict('os.environ',{'GIT_INDEX_FILE':str(index),'GIT_DIR':'/nonexistent','GIT_WORK_TREE':'/nonexistent'}):
            result=await worktree_base.capture('child-env',self.ws)
        self.assertTrue(result['ok'],result); self.assertEqual(index.read_bytes(),before)

    async def test_excluded_directory_is_not_in_capture(self):
        from engine import worktree_base
        (self.ws/'node_modules').mkdir(); (self.ws/'node_modules'/'x').write_text('excluded')
        result=await worktree_base.capture('child-05',self.ws); self.assertTrue(result['ok'],result)
        wt=await worktree.make(result['repo_key'],'child-05'); self.assertIsNotNone(wt)
        self.assertEqual((Path(wt)/'file.txt').read_text(),'current\n')
        self.assertFalse((Path(wt)/'node_modules'/'x').exists())

    async def test_unavailable_or_disabled_checkpoint_does_not_create_repo(self):
        from engine import worktree_base
        with patch.object(config,'load_config',return_value={'checkpoints_enabled':False}):
            result=await worktree_base.capture('child-06',self.ws)
        self.assertFalse(result['ok']); self.assertFalse((self.wss/'.ckpt-wtbase-child-06.git').exists())
        with patch.object(checkpoints,'GIT',None):
            result=await worktree_base.capture('child-07',self.ws)
        self.assertFalse(result['ok']); self.assertFalse((self.wss/'.ckpt-wtbase-child-07.git').exists())
