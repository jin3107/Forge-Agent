"""Production lifecycle hook output capture; POSIX shell fixtures."""
import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks


@unittest.skipUnless(os.name=='posix','shell command quoting tested on POSIX')
class HookRunnerOutput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve()

    async def run_cmd(self,script,payload=None):
        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        return await hooks._run_cmd(cmd,payload or {},self.ws)

    async def test_large_stdout_stderr_drained_bounded_and_indicated(self):
        script="import os; b=b'x'*8192; [(os.write(1,b),os.write(2,b)) for _ in range(512)]"
        with patch.object(hooks,'HOOK_CAPTURE_BYTES',4096,create=True):
            result=await asyncio.wait_for(self.run_cmd(script),5)
        self.assertEqual(result['code'],0)
        self.assertIn('[output truncated]',result['out']); self.assertIn('[output truncated]',result['err'])
        self.assertLessEqual(len(result['out']),1200); self.assertLessEqual(len(result['err']),600)

    async def test_short_output_payload_environment_and_exit_compatible(self):
        script="import json,sys,os; d=json.load(sys.stdin); print(d['x'],os.environ['FORGE_WORKSPACE']); print('error',file=sys.stderr); sys.exit(2)"
        result=await self.run_cmd(script,{'x':'hello'})
        self.assertEqual(result,{'code':2,'out':f'hello {self.ws}','err':'error'})

    async def test_early_stdin_close_and_output_are_handled(self):
        script="import os; os.close(0); os.write(1,b'finished')"
        result=await asyncio.wait_for(self.run_cmd(script,{'x':'x'*500000}),5)
        self.assertEqual(result['code'],0); self.assertEqual(result['out'],'finished')

    async def test_exact_cap_utf8_boundary_and_truncation(self):
        with patch.object(hooks,'HOOK_CAPTURE_BYTES',4,create=True):
            exact=await self.run_cmd("import os; os.write(1,b'abcd')")
            truncated=await self.run_cmd("import os; os.write(1,b'abc\\xe2\\x82\\xac')")
        self.assertEqual(exact['out'],'abcd')
        self.assertIn('abc�',truncated['out']); self.assertIn('[output truncated]',truncated['out'])

    async def test_display_limit_truncation_is_reported_even_below_byte_cap(self):
        result=await self.run_cmd("import os; os.write(1,b'x'*1201); os.write(2,b'y'*601)")
        self.assertIn('[output truncated]',result['out']); self.assertIn('[output truncated]',result['err'])
        self.assertLessEqual(len(result['out']),1200); self.assertLessEqual(len(result['err']),600)

    async def test_capture_helper_retains_only_prefix_while_draining(self):
        class Stream:
            def __init__(self): self.remaining=100000
            async def read(self,n):
                size=min(n,self.remaining); self.remaining-=size
                return b'x'*size
        stream=Stream()
        with patch.object(hooks,'HOOK_CAPTURE_BYTES',17):
            data,truncated=await hooks._hook_read_prefix(stream)
        self.assertEqual(data,b'x'*17); self.assertTrue(truncated); self.assertEqual(stream.remaining,0)

    async def test_input_and_both_outputs_are_processed_concurrently(self):
        script=("import os,sys,json; b=b'x'*8192; "
                "[(os.write(1,b),os.write(2,b)) for _ in range(256)]; "
                "d=json.load(sys.stdin); sys.exit(0 if len(d['x'])==500000 else 5)")
        result=await asyncio.wait_for(self.run_cmd(script,{'x':'x'*500000}),5)
        self.assertEqual(result['code'],0); self.assertIn('[output truncated]',result['out'])
        self.assertIn('[output truncated]',result['err'])

    async def test_timeout_output_compatibility(self):
        with patch.object(hooks,'TIMEOUT',.15):
            result=await asyncio.wait_for(self.run_cmd("import time; time.sleep(1)"),3)
        self.assertEqual(result['code'],1); self.assertIn('hook timeout',result['err'])
        self.assertEqual(result['out'],'')
