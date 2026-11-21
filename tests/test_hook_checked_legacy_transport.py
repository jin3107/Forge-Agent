"""Regression: prototype flood timeout should drain its killed child's pipe."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name=='posix','POSIX child cleanup tested only on POSIX')
class CheckedLegacyTransport(unittest.IsolatedAsyncioTestCase):
    async def test_flood_timeout_does_not_leave_unclosed_transport_in_controlled_case(self):
        with tempfile.TemporaryDirectory(prefix='forge-hook-transport-') as td:
            script=r'''
import asyncio,gc,shlex,sys
from pathlib import Path
from engine import hook_checked_runner as r
from engine.hook_approvals import HookApprovals
async def main():
    ws=Path(sys.argv[1]); reg=HookApprovals()
    r._LEGACY_TIMEOUT=.2; r._LEGACY_CAPTURE_BYTES=1024
    code="import os; b=b'x'*8192\nwhile True: os.write(1,b)"
    cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(code)}'
    result=await r.checked_global_file_change(ws,'o','s','file',lambda a,b,c:reg.snapshot(c,a,b),{'on_file_change':cmd})
    assert 'timeout' in result and 'cleanup unconfirmed' in result
asyncio.run(main())
gc.collect()
'''
            env=os.environ.copy(); env['PYTHONPATH']=str(ROOT)+os.pathsep+env.get('PYTHONPATH','')
            proc=await asyncio.create_subprocess_exec(sys.executable,'-W','always::ResourceWarning','-c',script,td,
                    cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                out,err=await asyncio.wait_for(proc.communicate(),10)
            finally:
                if proc.returncode is None: proc.kill(); await proc.wait()
            self.assertEqual(proc.returncode,0,err.decode())
            self.assertNotIn('ResourceWarning',err.decode())
            self.assertNotIn('unclosed transport',err.decode())
