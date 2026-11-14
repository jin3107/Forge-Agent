"""POSIX background process-group termination with real local subprocesses."""
import asyncio
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import background


@unittest.skipUnless(os.name == 'posix', 'POSIX process groups only')
class BackgroundProcessGroup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-bg-group-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.pidfile = self.ws / 'child.pid'
        self.marker = self.ws / 'survived'
        self.script = self.ws / 'spawn.py'
        self.script.write_text(
            "import os,pathlib,subprocess,sys,time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"alive\")', "
            "sys.argv[2]])\n"
            "pidpath = pathlib.Path(sys.argv[1])\n"
            "partial = pidpath.with_suffix('.part')\n"
            "pid = str(child.pid)\n"
            "with partial.open('w') as f:\n"
            "    f.write(pid[:1]); f.flush()\n"
            "    if len(sys.argv) > 3:\n"
            "        (pidpath.parent / 'pid-staged').touch()\n"
            "        while not (pidpath.parent / 'pid-release').exists(): time.sleep(0.01)\n"
            "    f.write(pid[1:])\n"
            "os.replace(partial, pidpath)\n"
            "print('started', flush=True)\n"
            "time.sleep(20)\n"
        )
        self.tid = None
        self.group_id = None
        self.child_pid = None
        self.pump_done = asyncio.Event()
        self.pump_waiting = asyncio.Event()
        self.pump_release = asyncio.Event()
        self.pause_pump_exit = False
        self.pump_started = False
        self.scope = {'owner_id': 'owner', 'session_id': 'session'}

    async def asyncTearDown(self):
        self.pump_release.set()
        await self._finish_fixture_processes()

    async def _finish_fixture_processes(self):
        # Clean up the known fixture group even if startup failed before PID
        # publication; never infer a signal target from a partial file.
        if self.group_id is not None:
            try:
                os.killpg(self.group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.tid:
            t = background.get(self.tid)
            if t and t['proc'].returncode is None:
                try:
                    t['proc'].kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(t['proc'].wait(), 3)
                except asyncio.TimeoutError:
                    pass
            if self.pump_started:
                await asyncio.wait_for(self.pump_done.wait(), 3)
            background._TASKS.pop(self.tid, None)
        self.tid = None
        self.group_id = None

    async def launch(self, leader_exits=False, shell=False, pause_publish=False):
        if leader_exits:
            self.script.write_text(self.script.read_text().replace('time.sleep(20)', 'sys.exit(0)'))
        argv = [str(x) for x in (sys.executable, self.script, self.pidfile, self.marker)]
        if pause_publish:
            argv.append('pause-publish')
        original_pump = background._pump
        async def tracked_pump(task):
            try:
                await original_pump(task)
            finally:
                if self.pause_pump_exit:
                    self.pump_waiting.set()
                    await self.pump_release.wait()
                self.pump_done.set()
        with patch.object(background, '_pump', tracked_pump):
            self.tid, _ = await background.start(shlex.join(argv) if shell else 'test fixture', self.ws,
                                                  **self.scope, exec_args=None if shell else argv)
        self.assertTrue(self.tid)
        self.pump_started = True
        self.group_id = background.get(self.tid)['proc'].pid
        async def ready():
            while not self.pidfile.exists():
                await asyncio.sleep(0.01)
            value = self.pidfile.read_text()
            self.assertTrue(value.isascii() and value.isdecimal(), f'invalid fixture PID: {value!r}')
            pid = int(value)
            self.assertGreater(pid, 1)
            return pid
        self.child_pid = await asyncio.wait_for(ready(), 3)

    async def test_pid_is_not_published_until_complete(self):
        launch = asyncio.create_task(self.launch(pause_publish=True))
        try:
            async def staged():
                while not (self.ws / 'pid-staged').exists():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(staged(), 3)
            self.assertFalse(self.pidfile.exists(), 'partial PID must not be visible as ready')
            self.assertEqual(len(self.pidfile.with_suffix('.part').read_text()), 1)
            self.assertFalse(launch.done(), 'launch must wait for complete publication')
            (self.ws / 'pid-release').touch()
            await asyncio.wait_for(launch, 3)
            self.assertGreater(self.child_pid, 1)
            self.assertEqual(int(self.pidfile.read_text()), self.child_pid)
            await background.kill(self.tid, self.ws, **self.scope)
        finally:
            (self.ws / 'pid-release').touch()
            if not launch.done():
                launch.cancel()
                await asyncio.gather(launch, return_exceptions=True)

    async def test_fixture_cleanup_waits_for_stdout_pump(self):
        self.pause_pump_exit = True
        await self.launch()
        proc = background.get(self.tid)['proc']
        await background.kill(self.tid, self.ws, **self.scope)
        await asyncio.wait_for(self.pump_waiting.wait(), 3)
        cleanup = asyncio.create_task(self._finish_fixture_processes())
        try:
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done(), 'fixture cleanup must wait for its output pump')
            self.pump_release.set()
            await asyncio.wait_for(cleanup, 3)
            self.assertTrue(self.pump_done.is_set())
            self.assertTrue(proc.stdout.at_eof())
        finally:
            self.pump_release.set()
            if not cleanup.done():
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

    async def test_kill_stops_same_group_descendant(self):
        await self.launch(shell=True)
        output = await background.kill(self.tid, self.ws, **self.scope)
        self.assertIn('kill', output.lower())
        self.assertEqual(background.get(self.tid)['status'], 'killed')
        await asyncio.sleep(2.2)
        self.assertFalse(self.marker.exists(), 'descendant continued after kill')

    async def test_kill_when_leader_exited_but_pipe_is_held(self):
        await self.launch(leader_exits=True)
        async def exited():
            while background.get(self.tid)['proc'].returncode is None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(exited(), 1)
        self.assertEqual(background.get(self.tid)['status'], 'running')
        await background.kill(self.tid, self.ws, **self.scope)
        await asyncio.sleep(2.2)
        self.assertFalse(self.marker.exists(), 'pipe-holding descendant survived kill')

    async def test_group_signal_failure_is_reported_when_direct_kill_succeeds(self):
        self.tid = 'bg-mock-fallback'
        proc = MagicMock(pid=123456, returncode=-9)
        proc.wait = AsyncMock(return_value=-9)
        background._TASKS[self.tid] = {'id': self.tid, 'cmd': 'fixture', 'proc': proc,
            'scope': background._scope(self.ws, **self.scope), 'status': 'running',
            'killed': False, 'code': None, 'started': time.time(), 'buf': bytearray(),
            'off': 0, 'base_offset': 0}
        with patch('engine.background.os.killpg', side_effect=PermissionError('denied')):
            result = await background.kill(self.tid, self.ws, **self.scope)
        proc.kill.assert_called_once()
        self.assertIn('chỉ gửi tín hiệu tới tiến trình trực tiếp', result)
        self.assertEqual(background.get(self.tid)['status'], 'killed')

    async def test_both_signal_failures_return_error_not_killed(self):
        self.tid = 'bg-mock-failure'
        proc = MagicMock(pid=123456, returncode=0)
        proc.kill.side_effect = PermissionError('denied')
        proc.wait = AsyncMock(return_value=0)
        background._TASKS[self.tid] = {'id': self.tid, 'cmd': 'fixture', 'proc': proc,
            'scope': background._scope(self.ws, **self.scope), 'status': 'running',
            'killed': False, 'code': None, 'started': time.time(), 'buf': bytearray(),
            'off': 0, 'base_offset': 0}
        with patch('engine.background.os.killpg', side_effect=PermissionError('denied')):
            result = await background.kill(self.tid, self.ws, **self.scope)
        self.assertTrue(result.startswith('Error'), result)
        self.assertFalse(background.get(self.tid)['killed'])
        self.assertEqual(background.get(self.tid)['status'], 'running')
        proc.wait.assert_not_awaited()

    async def test_unauthorized_kill_does_not_signal_group(self):
        await self.launch()
        result = await background.kill(self.tid, self.ws, owner_id='other', session_id='session')
        self.assertTrue(result.startswith('Error'), result)
        self.assertIsNone(background.get(self.tid)['proc'].returncode)
        await background.kill(self.tid, self.ws, **self.scope)


if __name__ == '__main__':
    unittest.main()
