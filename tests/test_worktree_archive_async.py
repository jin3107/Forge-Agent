"""Bounded async worktree archive capture; fork's separate sync path is not covered."""
import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config, worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeArchiveAsync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=self.wss/'parent'; self.ws.mkdir(); self.data=os.urandom(8000); (self.ws/'data.bin').write_bytes(self.data)
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.ws,'archive base')); self.dst=self.wss/'output'

    async def test_oversized_archive_is_refused_before_destination_creation(self):
        with patch.object(worktree,'WORKTREE_ARCHIVE_LIMIT',1024,create=True):
            result=await worktree.export_tree('parent',self.dst)
        self.assertFalse(result); self.assertFalse(self.dst.exists())

    async def test_valid_archive_exports_exact_binary_content(self):
        self.assertTrue(await worktree.export_tree('parent',self.dst))
        self.assertEqual((self.dst/'data.bin').read_bytes(),self.data)

    async def test_runner_error_or_timeout_cannot_publish_partial_archive(self):
        for result in ((125,b'',b'private'),(124,b'',b'timeout'),(1,b'partial',b'private')):
            with self.subTest(result=result),patch.object(worktree,'_run_binary_git',new_callable=AsyncMock,return_value=result):
                self.assertFalse(await worktree.export_tree('parent',self.dst)); self.assertFalse(self.dst.exists())

    async def test_runner_exception_is_sanitized_and_does_not_create_destination(self):
        with patch.object(worktree,'_run_binary_git',new_callable=AsyncMock,side_effect=OSError('private')):
            self.assertFalse(await worktree.export_tree('parent',self.dst))
        self.assertFalse(self.dst.exists())

    async def test_cancellation_propagates_without_creating_destination(self):
        entered=asyncio.Event()
        async def run(*args,**kwargs): entered.set(); await asyncio.Event().wait()
        with patch.object(worktree,'_run_binary_git',side_effect=run):
            task=asyncio.create_task(worktree.export_tree('parent',self.dst)); await asyncio.wait_for(entered.wait(),3); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.dst.exists())
