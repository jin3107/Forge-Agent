"""Bounded output experiments for the standalone global legacy hook adapter."""
import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hook_checked_runner as runner
from engine.hook_approvals import HookApprovals


@unittest.skipUnless(os.name == 'posix','shell commands tested on POSIX only')
class LegacyOutput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); self.registry=HookApprovals()

    def command(self,script): return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
    async def run_hook(self,script):
        def resolve(owner,sid,ws): return self.registry.snapshot(ws,owner,sid)
        return await runner.checked_global_file_change(self.ws,'owner','sid','file.txt',resolve,{'on_file_change':self.command(script)})

    async def test_large_output_drained_and_explicitly_truncated(self):
        script="import os; b=b'x'*8192; [(os.write(1,b)) for _ in range(512)]; os.write(2,b'END')"
        with patch.object(runner,'_LEGACY_CAPTURE_BYTES',4096,create=True):
            result=await asyncio.wait_for(self.run_hook(script),5)
        self.assertIn('exit 0',result); self.assertIn('output truncated',result)
        self.assertNotIn('END',result); self.assertLess(len(result),1000)

    async def test_short_merged_output_unchanged(self):
        result=await self.run_hook("import os; os.write(1,b'out'); os.write(2,b'err')")
        self.assertEqual(result,'\n🔗 Hook on_file_change (exit 0): outerr')

    async def test_limit_exact_and_incomplete_utf8_are_handled(self):
        with patch.object(runner,'_LEGACY_CAPTURE_BYTES',4,create=True):
            exact=await self.run_hook("import os; os.write(1,b'abcd')")
            split=await self.run_hook("import os; os.write(1,b'abc\\xe2\\x82\\xac')")
        self.assertNotIn('output truncated',exact)
        self.assertIn('abc�',split); self.assertIn('output truncated',split)

    async def test_output_over_display_limit_is_explicitly_truncated(self):
        result=await self.run_hook("import os; os.write(1,b'x'*601)")
        self.assertIn('output truncated',result)

    async def test_reader_error_attempts_cleanup_and_does_not_leak_details(self):
        started=self.ws/'started'; late=self.ws/'late'
        script=(f"from pathlib import Path; import time; Path({str(started)!r}).write_text('started'); "
                f"time.sleep(.7); Path({str(late)!r}).write_text('late')")
        async def broken(proc):
            async def wait():
                while not started.exists(): await asyncio.sleep(.01)
            await asyncio.wait_for(wait(),3)
            raise OSError('PRIVATE READER FAILURE')
        with patch.object(runner,'_capture_legacy_output',side_effect=broken):
            result=await self.run_hook(script)
        self.assertIn('execution or cleanup unconfirmed',result); self.assertNotIn('PRIVATE',result)
        await asyncio.sleep(.9); self.assertFalse(late.exists())

    async def test_flooding_child_timeout_stays_bounded_and_stops(self):
        script="import os; b=b'x'*8192\nwhile True: os.write(1,b)"
        with patch.object(runner,'_LEGACY_TIMEOUT',.2),patch.object(runner,'_LEGACY_CAPTURE_BYTES',1024,create=True):
            result=await asyncio.wait_for(self.run_hook(script),4)
        self.assertIn('timeout',result); self.assertIn('cleanup unconfirmed',result)
