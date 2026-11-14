"""Archive extraction stages first; invalid content must not reach destination."""
import io
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config, worktree


def archive(entries):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w') as tar:
        for name,data in entries:
            info=tarfile.TarInfo(name); info.size=len(data); tar.addfile(info,io.BytesIO(data))
    return buf.getvalue()

@unittest.skipUnless(shutil.which('git'),'Git required')
class ArchiveExtraction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=self.wss/'parent'; self.ws.mkdir(); (self.ws/'file').write_text('base')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.ws,'base')); self.dst=self.wss/'target'

    async def test_unsafe_late_member_leaves_no_destination_for_both_paths(self):
        payload=archive([('good.txt',b'good'),('../escape.txt',b'bad')])
        with patch.object(worktree,'_run_binary_git',new_callable=AsyncMock,return_value=(0,payload,b'')):
            self.assertFalse(await worktree.export_tree('parent',self.dst))
        self.assertFalse(self.dst.exists()); self.assertFalse((self.wss/'escape.txt').exists())
        with patch.object(worktree,'_run_binary_sync',return_value=(0,payload,b'')):
            self.assertFalse(worktree.export_tree_sync('parent',self.dst))
        self.assertFalse(self.dst.exists()); self.assertFalse((self.wss/'escape.txt').exists())
        self.assertFalse(any(p.name.startswith('.forge-archive-') for p in self.wss.iterdir()))

    async def test_truncated_archive_leaves_no_destination(self):
        payload=archive([('good.txt',b'good'),('later.bin',b'x'*4096)])[:2048]
        with patch.object(worktree,'_run_binary_sync',return_value=(0,payload,b'')):
            self.assertFalse(worktree.export_tree_sync('parent',self.dst))
        self.assertFalse(self.dst.exists())
        self.assertFalse(any(p.name.startswith('.forge-archive-') for p in self.wss.iterdir()))

    async def test_existing_destination_is_not_overwritten(self):
        self.dst.mkdir(); (self.dst/'file').write_text('keep')
        self.assertFalse(await worktree.export_tree('parent',self.dst)); self.assertEqual((self.dst/'file').read_text(),'keep')
        self.assertFalse(worktree.export_tree_sync('parent',self.dst)); self.assertEqual((self.dst/'file').read_text(),'keep')

    async def test_failed_publish_reports_failure_and_leaves_partial_destination(self):
        original=shutil.copytree
        def fail(src,dst,*args,**kwargs):
            Path(dst).mkdir(); (Path(dst)/'partial').write_text('partial'); raise OSError('private')
        with patch.object(worktree,'_rename_noreplace',return_value=False), patch.object(shutil,'copytree',side_effect=fail):
            self.assertFalse(worktree.export_tree_sync('parent',self.dst))
        self.assertEqual((self.dst/'partial').read_text(),'partial')
        self.assertFalse(any(p.name.startswith('.forge-archive-') for p in self.wss.iterdir()))
        _=original

    async def test_valid_sync_and_async_archives_export(self):
        self.assertTrue(await worktree.export_tree('parent',self.dst)); self.assertEqual((self.dst/'file').read_text(),'base')
        other=self.wss/'other'; self.assertTrue(worktree.export_tree_sync('parent',other)); self.assertEqual((other/'file').read_text(),'base')

    @unittest.skipUnless(os.name=='posix','symlink behavior tested on POSIX')
    async def test_valid_archive_preserves_relative_symlink(self):
        (self.ws/'link').symlink_to('file')
        self.assertIsNotNone(await checkpoints.snapshot('link-parent',self.ws,'base'))
        self.assertTrue(worktree.export_tree_sync('link-parent',self.dst))
        self.assertTrue((self.dst/'link').is_symlink()); self.assertEqual(os.readlink(self.dst/'link'),'file')
