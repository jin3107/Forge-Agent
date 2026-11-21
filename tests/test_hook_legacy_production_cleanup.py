"""Production file-change hook cleanup experiments; not sandbox or trust enforcement."""
import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import config, hooks, tools


@unittest.skipUnless(os.name=='posix','POSIX shell and process group tests')
class LegacyProductionCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve()

    def command(self,started,late):
        script=(f"from pathlib import Path; import time; Path({str(started)!r}).write_text('start'); "
                f"time.sleep(.8); Path({str(late)!r}).write_text('late')")
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    async def write(self,cmd):
        with patch('engine.checkpoints.snapshot',new_callable=AsyncMock), \
             patch('engine.lsp.available_for',return_value=False), \
             patch.object(config,'load_config',return_value={'on_file_change':cmd}):
            return await tools.execute_tool('write_file',{'path':'file.txt','content':'written'},self.ws)

    async def wait_started(self,path):
        async def wait():
            while not path.exists(): await asyncio.sleep(.01)
        await asyncio.wait_for(wait(),3)

    async def test_timeout_stops_same_group_child_but_write_is_not_undone(self):
        started=self.ws/'start'; late=self.ws/'late'
        with patch.object(tools,'LEGACY_HOOK_TIMEOUT',.25):
            task=asyncio.create_task(self.write(self.command(started,late)))
            try:
                await self.wait_started(started)
                result=await asyncio.wait_for(task,3)
                self.assertTrue(result['ok']); self.assertIn('exit 124',result['output'])
                self.assertIn('cleanup unconfirmed',result['output'])
                self.assertEqual((self.ws/'file.txt').read_text(),'written')
                await asyncio.sleep(1); self.assertFalse(late.exists())
            finally:
                if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_cancellation_propagates_and_stops_same_group_child(self):
        started=self.ws/'start'; late=self.ws/'late'
        task=asyncio.create_task(self.write(self.command(started,late)))
        try:
            await self.wait_started(started); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,3)
            self.assertEqual((self.ws/'file.txt').read_text(),'written')
            await asyncio.sleep(1); self.assertFalse(late.exists())
        finally:
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_read_failure_cleans_up_and_keeps_existing_tool_error_behavior(self):
        started=self.ws/'start'; late=self.ws/'late'
        async def broken(proc):
            await self.wait_started(started); raise OSError('fixture reader error')
        with patch.object(hooks,'_hook_capture_merged',side_effect=broken):
            result=await self.write(self.command(started,late))
        self.assertTrue(result['ok']); self.assertIn('on_file_change lỗi',result['output'])
        self.assertEqual((self.ws/'file.txt').read_text(),'written')
        await asyncio.sleep(1); self.assertFalse(late.exists())

    async def test_escaped_descendant_can_survive_timeout(self):
        started=self.ws/'start'; late=self.ws/'escaped'
        child=f"from pathlib import Path; import time; time.sleep(.8); Path({str(late)!r}).write_text('escaped')"
        script=(f"import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"Path({str(started)!r}).write_text('start'); time.sleep(5)")
        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        with patch.object(tools,'LEGACY_HOOK_TIMEOUT',.25):
            task=asyncio.create_task(self.write(cmd))
            try:
                await self.wait_started(started); result=await asyncio.wait_for(task,3)
                self.assertIn('cleanup unconfirmed',result['output'])
                await asyncio.sleep(1); self.assertTrue(late.exists())
            finally:
                if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
