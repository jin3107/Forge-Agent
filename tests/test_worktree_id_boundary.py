"""Direct worktree helpers reject malformed path/ref keys before filesystem or Git."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import config, worktree

class WorktreeIDBoundary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); ws=root/'workspaces'; ws.mkdir(); sessions=root/'sessions'; sessions.mkdir(); self.ws=ws
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=ws,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        (ws/'.ckpt-valid.git').mkdir(); (ws/'wt-valid').mkdir()
        self.bad=('../valid','valid/../other','valid.', '', 'x'*300, 'a\x00b')

    async def test_path_constructors_reject_ids_instead_of_building_paths(self):
        for bad in self.bad:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError): worktree.wt_path(bad)
                with self.assertRaises(ValueError): worktree._repo(bad)
        self.assertEqual(worktree.wt_path('child-1'),self.ws/'wt-child-1')
        self.assertEqual(worktree._repo('wtbase-child-1'),self.ws/'.ckpt-wtbase-child-1.git')

    async def test_invalid_make_and_available_never_call_git(self):
        for bad in self.bad:
            with self.subTest(bad=bad), patch.object(worktree,'_run',new_callable=AsyncMock) as run:
                self.assertFalse(await worktree.available_for(bad))
                self.assertIsNone(await worktree.make(bad,'valid'))
                self.assertIsNone(await worktree.make('valid',bad))
                run.assert_not_awaited()

    async def test_invalid_diff_merge_discard_never_call_git(self):
        for bad in self.bad:
            for sub,src,parent in ((bad,'valid','valid'),('valid',bad,'valid'),('valid','valid',bad)):
                with self.subTest(bad=bad,sub=sub,src=src,parent=parent), patch.object(worktree,'_run',new_callable=AsyncMock) as run:
                    if parent=='valid':
                        self.assertEqual(await worktree.diff_stat(sub,src),'')
                        self.assertFalse((await worktree.discard(sub,src))['ok'])
                    self.assertFalse((await worktree.merge(sub,src,parent,self.ws))['ok'])
                    run.assert_not_awaited()

    async def test_invalid_exports_do_not_spawn_git_or_create_destination(self):
        for bad in self.bad:
            dst=self.ws/'destination'
            with self.subTest(bad=bad), patch('engine.worktree.asyncio.create_subprocess_exec',new_callable=AsyncMock) as spawn, \
                 patch('subprocess.run') as sync:
                self.assertFalse(await worktree.export_tree(bad,dst))
                self.assertFalse(worktree.export_tree_sync(bad,dst))
                spawn.assert_not_awaited(); sync.assert_not_called(); self.assertFalse(dst.exists())

    async def test_list_all_skips_invalid_worktree_names(self):
        (self.ws/'wt-bad.name').mkdir(); (self.ws/'wt-').mkdir()
        rows=await worktree.list_all()
        self.assertFalse(any(row['session'] in ('bad.name','') for row in rows))
