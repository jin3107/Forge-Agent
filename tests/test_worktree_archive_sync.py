"""Bound synchronous archive capture and characterize fork fallback."""
import os
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import checkpoints, config, worktree
from engine.session import SessionStore, ScopedSessionStore

class SyncBinaryRunner(unittest.TestCase):
    def test_large_both_streams_drained_without_retaining_full_output(self):
        code="import sys; sys.stdout.buffer.write(b'x'*6000000); sys.stderr.buffer.write(b'y'*6000000)"
        tracemalloc.start()
        try:
            rc,out,err=worktree._run_binary_sync(sys.executable,['-c',code],dict(os.environ),stdout_limit=1024,stderr_limit=1024,timeout=5)
            _,peak=tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        self.assertEqual(rc,125); self.assertEqual(out,b''); self.assertEqual(len(err),1024)
        self.assertLess(peak,4_000_000)

    def test_exact_binary_and_exit_code_preserved(self):
        code="import sys; sys.stdout.buffer.write(b'\\x00\\xffab'); sys.stderr.write('err'); sys.exit(7)"
        rc,out,err=worktree._run_binary_sync(sys.executable,['-c',code],dict(os.environ),stdout_limit=4,stderr_limit=3,timeout=5)
        self.assertEqual((rc,out,err),(7,b'\0\xffab',b'err'))

    @unittest.skipUnless(os.name=='posix','POSIX signals')
    def test_descendant_holding_pipes_does_not_return_false_success(self):
        import signal
        with tempfile.TemporaryDirectory() as td:
            pidfile=Path(td)/'pid'
            code=("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                  "open(sys.argv[1],'w').write(str(p.pid))")
            try:
                with patch.object(worktree,'WORKTREE_GIT_CLEANUP_TIMEOUT',.2):
                    rc,out,err=worktree._run_binary_sync(sys.executable,['-c',code,str(pidfile)],dict(os.environ),stdout_limit=10,stderr_limit=10,timeout=5)
                self.assertEqual(rc,125); self.assertEqual(out,b'')
            finally:
                if pidfile.exists():
                    try: os.kill(int(pidfile.read_text()),signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_timeout_kills_and_reaps_direct_process(self):
        processes=[]; original=subprocess.Popen
        def spawn(*args,**kwargs):
            proc=original(*args,**kwargs); processes.append(proc); return proc
        with patch('subprocess.Popen',side_effect=spawn):
            rc,out,err=worktree._run_binary_sync(sys.executable,['-c','import time; time.sleep(30)'],dict(os.environ),stdout_limit=10,stderr_limit=10,timeout=.2)
        self.assertEqual(rc,124); self.assertEqual(out,b''); self.assertIsNotNone(processes[0].poll())

@unittest.skipUnless(shutil.which('git'),'Git required')
class SyncArchive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); self.session=self.store.create(model='test')
        self.ws=self.store.workspace(self.session.id); self.data=os.urandom(8000); (self.ws/'data.bin').write_bytes(self.data)
        self.assertIsNotNone(await checkpoints.snapshot(self.session.id,self.ws,'base')); self.dst=self.wss/'output'

    async def test_export_rejects_large_archive_before_creating_destination(self):
        with patch.object(worktree,'WORKTREE_ARCHIVE_LIMIT',1024):
            self.assertFalse(worktree.export_tree_sync(self.session.id,self.dst))
        self.assertFalse(self.dst.exists())

    async def test_valid_archive_restores_exact_snapshot_bytes(self):
        (self.ws/'data.bin').write_bytes(b'current')
        self.assertTrue(worktree.export_tree_sync(self.session.id,self.dst))
        self.assertEqual((self.dst/'data.bin').read_bytes(),self.data)

    async def test_runner_failure_and_exception_do_not_extract(self):
        for rc in (124,125,1):
            with self.subTest(rc=rc),patch.object(worktree,'_run_binary_sync',return_value=(rc,b'partial',b'private')):
                self.assertFalse(worktree.export_tree_sync(self.session.id,self.dst)); self.assertFalse(self.dst.exists())
        with patch.object(worktree,'_run_binary_sync',side_effect=OSError('private')):
            self.assertFalse(worktree.export_tree_sync(self.session.id,self.dst)); self.assertFalse(self.dst.exists())

    async def test_fork_refuses_live_fallback_when_archive_overflows(self):
        from engine.session import ForkError
        (self.ws/'data.bin').write_bytes(b'newer current state')
        with patch.object(worktree,'WORKTREE_ARCHIVE_LIMIT',1024):
            with self.assertRaises(ForkError) as failure:
                self.store.fork(self.session.id)
        self.assertFalse((self.wss/failure.exception.child_sid/'data.bin').exists())
        self.assertEqual((self.ws/'data.bin').read_bytes(),b'newer current state')
