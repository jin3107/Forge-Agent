"""POSIX subprocess lifecycle experiments for the unused legacy adapter."""
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


@unittest.skipUnless(os.name == 'posix', 'POSIX process groups only')
class LegacyCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); self.approvals=HookApprovals()

    def command(self, started, late):
        # Child remains in the shell's process group and holds stdout open.
        script=(f"from pathlib import Path; import time; Path({str(started)!r}).write_text('started'); "
                f"time.sleep(0.7); Path({str(late)!r}).write_text('late')")
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    async def run_hook(self,command):
        def resolve(owner,sid,ws): return self.approvals.snapshot(ws,owner,sid)
        return await runner.checked_global_file_change(self.ws,'owner','sid','file.txt',resolve,{'on_file_change':command})

    async def wait_started(self,path):
        async def wait():
            while not path.exists(): await asyncio.sleep(.01)
        await asyncio.wait_for(wait(),3)

    async def test_cancel_kills_same_group_child_and_propagates(self):
        started=self.ws/'started'; late=self.ws/'late'
        task=asyncio.create_task(self.run_hook(self.command(started,late)))
        try:
            await self.wait_started(started)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,3)
            await asyncio.sleep(.9)
            self.assertFalse(late.exists(), 'child continued after cancellation')
        finally:
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_detached_descendant_can_survive_cancellation(self):
        started=self.ws/'started'; late=self.ws/'escaped'
        child=f"from pathlib import Path; import time; time.sleep(.7); Path({str(late)!r}).write_text('escaped')"
        parent=(f"import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"Path({str(started)!r}).write_text('started'); time.sleep(5)")
        task=asyncio.create_task(self.run_hook(f'{shlex.quote(sys.executable)} -c {shlex.quote(parent)}'))
        try:
            await self.wait_started(started); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,3)
            await asyncio.sleep(1)
            self.assertTrue(late.exists(), 'this test explicitly demonstrates escaped descendants')
        finally:
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_timeout_kills_same_group_child_and_reports_timeout(self):
        started=self.ws/'started'; late=self.ws/'late'
        with patch.object(runner,'_LEGACY_TIMEOUT',.25,create=True):
            task=asyncio.create_task(self.run_hook(self.command(started,late)))
            try:
                await self.wait_started(started)
                result=await asyncio.wait_for(task,2)
                self.assertIn('timeout',result)
                await asyncio.sleep(.9)
                self.assertFalse(late.exists(), 'child continued after timeout')
            finally:
                if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
