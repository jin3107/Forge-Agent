"""Bounded binary Git apply, with explicit uncertain-side-effect reporting."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree

class BinaryApplyRunner(unittest.IsolatedAsyncioTestCase):
    async def test_input_bytes_and_outputs_are_bounded_and_separate(self):
        code="import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'e'*20)"
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=4,stderr_limit=20,timeout=5,input_data=b'\0\xffab')
        self.assertEqual((rc,out,err),(0,b'\0\xffab',b'e'*20))
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=3,stderr_limit=20,timeout=5,input_data=b'\0\xffab')
        self.assertEqual(rc,125); self.assertEqual(out,b'')

    async def test_output_flood_during_input_write_does_not_deadlock(self):
        code="import sys; sys.stdout.buffer.write(b'x'*3000000); sys.stdout.flush(); sys.stderr.buffer.write(b'y'*3000000); sys.stderr.flush(); sys.stdin.buffer.read()"
        rc,out,err=await asyncio.wait_for(worktree._run_binary_git(sys.executable,['-c',code],{},stdout_limit=1000,stderr_limit=1000,timeout=5,input_data=b'z'*1000000),10)
        self.assertEqual(rc,125); self.assertEqual(out,b''); self.assertEqual(len(err),1000)

    async def test_timeout_and_cancellation_with_blocked_stdin_reap_child(self):
        processes=[]; started=asyncio.Event(); original=asyncio.create_subprocess_exec
        async def spawn(*args,**kwargs):
            proc=await original(*args,**kwargs); processes.append(proc); started.set(); return proc
        args=['-c','import time; time.sleep(30)']
        try:
            with patch('asyncio.create_subprocess_exec',side_effect=spawn):
                rc,out,err=await asyncio.wait_for(worktree._run_binary_git(sys.executable,args,{},stdout_limit=100,stderr_limit=100,timeout=.2,input_data=b'z'*1000000),6)
                self.assertEqual(rc,124); self.assertIsNotNone(processes[-1].returncode)
                started.clear(); task=asyncio.create_task(worktree._run_binary_git(sys.executable,args,{},stdout_limit=100,stderr_limit=100,timeout=5,input_data=b'z'*1000000))
                await asyncio.wait_for(started.wait(),3); await asyncio.sleep(.05); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertIsNotNone(processes[-1].returncode)
        finally:
            for proc in processes:
                if proc.returncode is None: proc.kill(); await proc.wait()

    async def test_zero_exit_with_unwritten_input_is_not_reported_success(self):
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c','import sys; sys.exit(0)'],{},stdout_limit=100,stderr_limit=100,timeout=5,input_data=b'x'*1000000)
        self.assertEqual(rc,125); self.assertEqual(out,b'')

    async def test_child_early_exit_is_nonzero_not_false_success(self):
        rc,out,err=await worktree._run_binary_git(sys.executable,['-c',"import sys; sys.stderr.write('no'); sys.exit(7)"],{},stdout_limit=100,stderr_limit=100,timeout=5,input_data=b'x'*1000000)
        self.assertEqual(rc,7); self.assertEqual(err,b'no')

@unittest.skipUnless(shutil.which('git'),'Git required')
class MergeApplyFailure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); wss=root/'workspaces'; wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.ws=wss/'parent'; self.ws.mkdir(); (self.ws/'file').write_text('before\n')
        self.assertIsNotNone(await checkpoints.snapshot('parent',self.ws,'base'))
        self.wt=Path(await worktree.make('parent','child')); (self.wt/'file').write_text('child\n')

    async def test_apply_timeout_reports_possible_partial_effect(self):
        original=worktree._run_binary_git
        async def run(executable,args,env,**kwargs):
            if 'apply' in args:
                (self.ws/'file').write_text('partial change\n')
                return 124,b'',b'timeout'
            return await original(executable,args,env,**kwargs)
        with patch.object(worktree,'_run_binary_git',side_effect=run):
            result=await worktree.merge('child','parent','parent',self.ws)
        self.assertFalse(result['ok'],result); self.assertTrue(result['checkpoint_created'])
        self.assertIn('có thể',result['message'].lower()); self.assertEqual((self.ws/'file').read_text(),'partial change\n')

    async def test_apply_output_overflow_and_exception_report_possible_partial_effect(self):
        original=worktree._run_binary_git
        for failure in ('overflow','exception'):
            async def run(executable,args,env,**kwargs):
                if 'apply' in args:
                    if failure=='exception': raise OSError('private detail')
                    return 125,b'',b'private detail'
                return await original(executable,args,env,**kwargs)
            with self.subTest(failure=failure),patch.object(worktree,'_run_binary_git',side_effect=run):
                result=await worktree.merge('child','parent','parent',self.ws)
            self.assertFalse(result['ok'],result); self.assertTrue(result['checkpoint_created'])
            self.assertIn('có thể',result['message'].lower()); self.assertNotIn('private detail',result['message'])
