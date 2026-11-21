"""Real POSIX lifecycle-hook timeout/cancellation cleanup; no sandbox claim."""
import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks


@unittest.skipUnless(os.name=='posix','POSIX process-group experiments')
class HookRunnerCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve()

    def command(self,started,late):
        script=(f"from pathlib import Path; import time; Path({str(started)!r}).write_text('start'); "
                f"time.sleep(.8); Path({str(late)!r}).write_text('late')")
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    async def wait_marker(self,path):
        async def wait():
            while not path.exists(): await asyncio.sleep(.01)
        await asyncio.wait_for(wait(),3)

    async def test_timeout_stops_same_group_child_and_reports_uncertainty(self):
        started=self.ws/'start'; late=self.ws/'late'
        with patch.object(hooks,'TIMEOUT',.25):
            task=asyncio.create_task(hooks._run_cmd(self.command(started,late),{},self.ws))
            try:
                await self.wait_marker(started)
                result=await asyncio.wait_for(task,3)
                self.assertEqual(result['code'],1); self.assertIn('cleanup unconfirmed',result['err'])
                await asyncio.sleep(1); self.assertFalse(late.exists())
            finally:
                if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_cancel_stops_same_group_child_and_propagates(self):
        started=self.ws/'start'; late=self.ws/'late'
        task=asyncio.create_task(hooks._run_cmd(self.command(started,late),{},self.ws))
        try:
            await self.wait_marker(started); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,3)
            await asyncio.sleep(1); self.assertFalse(late.exists())
        finally:
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_reader_failure_after_spawn_attempts_cleanup(self):
        started=self.ws/'start'; late=self.ws/'late'
        async def broken(proc,data):
            await self.wait_marker(started)
            raise OSError('test read failure')
        with patch.object(hooks,'_hook_communicate_bounded',side_effect=broken):
            result=await hooks._run_cmd(self.command(started,late),{},self.ws)
        self.assertEqual(result['code'],1); self.assertIn('hook error',result['err'])
        await asyncio.sleep(1); self.assertFalse(late.exists())

    async def test_cleanup_failure_is_bounded_and_not_claimed_successful(self):
        from unittest.mock import AsyncMock, Mock
        proc=Mock(pid=4321,returncode=None)
        proc.kill.side_effect=PermissionError('not allowed')
        async def wait_forever(): await asyncio.Event().wait()
        proc.wait=AsyncMock(side_effect=wait_forever)
        with patch.object(hooks.os,'killpg',side_effect=PermissionError('not allowed')), \
             patch.object(hooks,'_HOOK_REAP_TIMEOUT',.02):
            self.assertIsNone(await asyncio.wait_for(hooks._stop_hook_process(proc),1))
        proc.kill.assert_called_once(); proc.wait.assert_awaited_once()

    async def test_exited_leader_is_not_signalled_by_reused_pid(self):
        from unittest.mock import AsyncMock, Mock
        proc=Mock(pid=4321,returncode=0); proc.wait=AsyncMock(return_value=0)
        with patch.object(hooks.os,'killpg') as kill_group:
            await hooks._stop_hook_process(proc)
        kill_group.assert_not_called(); proc.kill.assert_not_called()

    async def test_detached_descendant_may_survive_timeout(self):
        started=self.ws/'start'; late=self.ws/'escaped'
        child=f"from pathlib import Path; import time; time.sleep(.8); Path({str(late)!r}).write_text('escaped')"
        script=(f"import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"Path({str(started)!r}).write_text('started'); time.sleep(5)")
        command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        with patch.object(hooks,'TIMEOUT',.25):
            task=asyncio.create_task(hooks._run_cmd(command,{},self.ws))
            try:
                await self.wait_marker(started)
                result=await asyncio.wait_for(task,3)
                self.assertIn('cleanup unconfirmed',result['err'])
                await asyncio.sleep(1); self.assertTrue(late.exists())
            finally:
                if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
