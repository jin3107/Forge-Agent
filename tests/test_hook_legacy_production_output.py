"""Production execute_tool legacy output capture with real POSIX child processes."""
import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import config, hooks, tools


@unittest.skipUnless(os.name=='posix','shell command quoting tested on POSIX')
class LegacyProductionOutput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve()

    def command(self,script): return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
    async def write(self,script):
        with patch('engine.checkpoints.snapshot',new_callable=AsyncMock), \
             patch('engine.lsp.available_for',return_value=False), \
             patch.object(config,'load_config',return_value={'on_file_change':self.command(script)}):
            return await tools.execute_tool('write_file',{'path':'file.txt','content':'written'},self.ws,owner_id='owner',session_id='sid')

    async def test_large_output_drained_bounded_and_indicated(self):
        script="import os; b=b'x'*8192; [os.write(1,b) for _ in range(512)]; os.write(2,b'END')"
        with patch.object(hooks,'HOOK_CAPTURE_BYTES',4096):
            result=await asyncio.wait_for(self.write(script),5)
        self.assertTrue(result['ok']); self.assertIn('exit 0',result['output'])
        self.assertIn('[output truncated]',result['output']); self.assertNotIn('END',result['output'])
        self.assertEqual((self.ws/'file.txt').read_text(),'written')
        self.assertLess(len(result['output']),1000)

    async def test_short_output_merged_environment_and_exit_preserved(self):
        script=("import os; os.write(1,os.environ['FORGE_CHANGED_FILE'].encode()); "
                "os.write(2,b' error'); raise SystemExit(3)")
        result=await self.write(script)
        self.assertTrue(result['ok']); self.assertIn('Hook on_file_change (exit 3): file.txt error',result['output'])
        self.assertNotIn('[output truncated]',result['output'])

    async def test_exact_cap_and_split_utf8(self):
        with patch.object(hooks,'HOOK_CAPTURE_BYTES',4):
            exact=await self.write("import os; os.write(1,b'abcd')")
            split=await self.write("import os; os.write(1,b'abc\\xe2\\x82\\xac')")
        self.assertIn('exit 0): abcd',exact['output']); self.assertNotIn('[output truncated]',exact['output'])
        self.assertIn('abc�',split['output']); self.assertIn('[output truncated]',split['output'])

    async def test_display_limit_has_marker_and_is_bounded(self):
        result=await self.write("import os; os.write(1,b'x'*601)")
        hook=result['output'].split('Hook on_file_change (exit 0): ',1)[1].split('\n\nWarning:',1)[0]
        self.assertIn('[output truncated]',hook)
        self.assertLessEqual(len(hook),600)

    async def test_timeout_is_reported_without_undoing_write(self):
        with patch.object(tools,'LEGACY_HOOK_TIMEOUT',.15):
            result=await asyncio.wait_for(self.write("import time; time.sleep(0.7)"),3)
        self.assertTrue(result['ok']); self.assertIn('exit 124',result['output'])
        self.assertIn('timeout 0.15s',result['output'])
        self.assertEqual((self.ws/'file.txt').read_text(),'written')
        # A killed shell can leave its child alive; allow the short fixture child to exit.
        await asyncio.sleep(.8)

    async def test_capture_reader_failure_preserves_written_file_and_tool_status(self):
        with patch.object(hooks,'_hook_capture_merged',side_effect=OSError('fixture read failure')):
            result=await self.write('pass')
        self.assertTrue(result['ok']); self.assertIn('Hook on_file_change lỗi:',result['output'])
        self.assertEqual((self.ws/'file.txt').read_text(),'written')
        await asyncio.sleep(.1)

    async def test_no_output_placeholder(self):
        result=await self.write('pass')
        self.assertIn('Hook on_file_change (exit 0): (không output)',result['output'])
