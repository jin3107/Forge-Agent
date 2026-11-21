"""POSIX foreground process-group cleanup; descendants stay in the launched group."""
import asyncio
import os
import shlex
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import tools


@unittest.skipUnless(os.name == 'posix', 'process groups are POSIX-specific')
class ForegroundProcessGroup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-process-group-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.child_pid = self.ws / 'child.pid'
        self.marker = self.ws / 'survived'
        self.script = self.ws / 'spawn.py'
        self.script.write_text(
            "import subprocess,sys,time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"alive\")', "
            "sys.argv[2]])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "print('started', flush=True)\n"
            "time.sleep(20)\n"
        )
        self.cmd = ' '.join(shlex.quote(str(x)) for x in
                            (sys.executable, self.script, self.child_pid, self.marker))

    async def asyncTearDown(self):
        if self.child_pid.exists():
            try:
                os.kill(int(self.child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def wait_for_file(self, path):
        async def loop():
            while not path.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(loop(), 3)

    async def test_timeout_kills_descendant_in_same_group(self):
        with patch('engine.tools._prepare_command', return_value=(None, None)):
            result = await tools._run_command_result(self.ws, {'command': self.cmd, 'timeout_sec': 1})
        self.assertTrue(result['timed_out'], result)
        self.assertTrue(self.child_pid.exists())
        await asyncio.sleep(1.3)
        self.assertFalse(self.marker.exists(), 'descendant continued after timeout')

    async def test_timeout_kills_group_even_if_leader_exited(self):
        self.script.write_text(self.script.read_text().replace('time.sleep(20)', 'sys.exit(0)'))
        with patch('engine.tools._prepare_command', return_value=(None, None)):
            result = await tools._run_command_result(self.ws, {'command': self.cmd, 'timeout_sec': 1})
        self.assertTrue(result['timed_out'], result)
        self.assertTrue(self.child_pid.exists())
        await asyncio.sleep(1.3)
        self.assertFalse(self.marker.exists(), 'pipe-holding descendant survived leader exit')

    async def test_cancellation_kills_descendant_in_same_group(self):
        with patch('engine.tools._prepare_command', return_value=(None, None)):
            task = asyncio.create_task(tools._run_command_result(self.ws, {'command': self.cmd, 'timeout_sec': 10}))
            await self.wait_for_file(self.child_pid)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.sleep(2.2)
        self.assertFalse(self.marker.exists(), 'descendant continued after cancellation')

    async def test_normal_exit_still_returns_output(self):
        command = shlex.quote(sys.executable) + " -c " + shlex.quote("print('group-ok')")
        with patch('engine.tools._prepare_command', return_value=(None, None)):
            result = await tools._run_command_result(self.ws, {'command': command})
        self.assertTrue(result['ok'], result)
        self.assertIn('group-ok', result['output'])


if __name__ == '__main__':
    unittest.main()
