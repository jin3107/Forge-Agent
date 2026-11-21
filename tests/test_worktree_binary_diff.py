"""Real-Git merge diff uses bounded raw bytes and fails closed on truncation."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree

@unittest.skipUnless(shutil.which('git'),'Git required')
class WorktreeBinaryDiff(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); wsdir=root/'workspaces'; wsdir.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=wsdir,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.parent=wsdir/'parent'; self.parent.mkdir(); (self.parent/'file.bin').write_bytes(b'base\0binary\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.parent,'base'))
        self.wt=Path(await worktree.make('parent','child'))

    async def test_large_patch_is_refused_without_parent_checkpoint_or_apply(self):
        (self.wt/'file.bin').write_bytes(os.urandom(8000))
        with patch.object(worktree,'WORKTREE_PATCH_LIMIT',1024,create=True):
            result=await worktree.merge('child','parent','parent',self.parent)
        self.assertFalse(result['ok'],result); self.assertIn('diff',result['message'].lower())
        self.assertEqual((self.parent/'file.bin').read_bytes(),b'base\0binary\n')
        self.assertFalse(await worktree.available_for(checkpoints.key_for(self.parent)))

    async def test_small_binary_patch_and_undo_preserve_bytes(self):
        child=b'new\0binary\xffdata\n'; (self.wt/'file.bin').write_bytes(child)
        result=await worktree.merge('child','parent','parent',self.parent)
        self.assertTrue(result['ok'],result); self.assertEqual((self.parent/'file.bin').read_bytes(),child)
        self.assertTrue((await checkpoints.undo(checkpoints.key_for(self.parent),self.parent))['ok'])
        self.assertEqual((self.parent/'file.bin').read_bytes(),b'base\0binary\n')

class BinaryDiffRunner(unittest.IsolatedAsyncioTestCase):
    async def test_large_stdout_and_stderr_are_drained_and_rejected(self):
        code="import sys; sys.stdout.buffer.write(b'x'*4000000); sys.stderr.buffer.write(b'y'*4000000)"
        rc,out,err=await asyncio.wait_for(worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=1024,stderr_limit=1024,timeout=5),10)
        self.assertEqual(rc,125); self.assertEqual(out,b''); self.assertEqual(len(err),1024)

    async def test_cancellation_during_spawn_tracks_late_child(self):
        entered=asyncio.Event(); release=asyncio.Event(); processes=[]; original=asyncio.create_subprocess_exec
        async def spawn(*args,**kwargs):
            entered.set(); await release.wait(); proc=await original(*args,**kwargs); processes.append(proc); return proc
        try:
            with patch('asyncio.create_subprocess_exec',side_effect=spawn):
                task=asyncio.create_task(worktree._run_binary_git(sys.executable,['-c','import time; time.sleep(30)'],{},stdout_limit=10,stderr_limit=10,timeout=5))
                await asyncio.wait_for(entered.wait(),3); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertTrue(checkpoints._GIT_CLEANUPS)
                release.set(); self.assertEqual(await asyncio.wait_for(checkpoints.wait_git_cleanups(),5),0)
                self.assertEqual(len(processes),1); self.assertIsNotNone(processes[0].returncode)
        finally:
            release.set()
            for proc in processes:
                if proc.returncode is None: proc.kill(); await proc.wait()

    async def test_stdout_and_stderr_caps_and_raw_bytes(self):
        code="import sys; sys.stdout.buffer.write(b'\\x00\\xffabc'); sys.stderr.buffer.write(b'e'*33)"
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=5,stderr_limit=32,timeout=3)
        self.assertEqual(rc,125); self.assertEqual(out,b''); self.assertLessEqual(len(err),32)
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=5,stderr_limit=33,timeout=3)
        self.assertEqual(rc,0); self.assertEqual(out,b'\0\xffabc'); self.assertEqual(err,b'e'*33)

    async def test_timeout_and_cancel_reap_direct_child(self):
        processes=[]; started=asyncio.Event(); original=asyncio.create_subprocess_exec
        async def spawn(*args,**kwargs):
            proc=await original(*args,**kwargs); processes.append(proc); started.set(); return proc
        try:
            with patch('asyncio.create_subprocess_exec',side_effect=spawn):
                rc,out,err=await worktree._run_binary_git(sys.executable,['-c','import time; time.sleep(30)'],{},stdout_limit=10,stderr_limit=10,timeout=.2)
                self.assertEqual(rc,124); self.assertIsNotNone(processes[-1].returncode)
                started.clear(); task=asyncio.create_task(worktree._run_binary_git(sys.executable,['-c','import time; time.sleep(30)'],{},stdout_limit=10,stderr_limit=10,timeout=5))
                await asyncio.wait_for(started.wait(),3); await asyncio.sleep(.05); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertIsNotNone(processes[-1].returncode)
        finally:
            for proc in processes:
                if proc.returncode is None: proc.kill(); await proc.wait()
