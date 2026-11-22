"""Windows: every stop path ends the whole command tree, not only the cmd.exe wrapper.

Each test starts ``cmd.exe /c python parent.py``; the parent starts a grandchild that touches a
heartbeat file (it appends one byte every 50 ms). A stop is only successful when the heartbeat stops.
"""
import atexit
import asyncio
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-proctree-test-")
atexit.register(lambda: _tmp.cleanup())
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import background, hooks, proctree, tools  # noqa: E402

WINDOWS = sys.platform == "win32"

GRANDCHILD = ("import pathlib,sys,time\n"
              "p=pathlib.Path(sys.argv[1])\n"
              "end=time.time()+40\n"
              "while time.time()<end:\n"
              "    with p.open('a') as f:\n"
              "        f.write('x')\n"
              "    time.sleep(0.05)\n")
PARENT = ("import subprocess,sys,time\n"
          "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
          "time.sleep(40)\n")


class Fixture:
    def __init__(self, directory: Path):
        self.beat = directory / "beat.txt"
        self.grandchild = directory / "grandchild.py"
        self.parent = directory / "parent.py"
        self.grandchild.write_text(GRANDCHILD)
        self.parent.write_text(PARENT)
        self.command = f'"{sys.executable}" "{self.parent}" "{self.grandchild}" "{self.beat}"'

    async def wait_started(self):
        for _ in range(200):
            if self.beat.exists():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("grandchild never started")

    async def assert_beat_stops(self):
        await asyncio.sleep(0.3)
        first = self.beat.stat().st_size
        await asyncio.sleep(0.6)
        self.beat_alive = self.beat.stat().st_size != first
        assert not self.beat_alive, "grandchild still running after stop"

    def force_stop(self):
        # Safety net so a failing test cannot leave a 40 s grandchild behind.
        # Match the full path of this fixture's script (in a fresh temporary folder) and leave out the
        # PowerShell process that runs the query, whose own command line contains that path.
        import subprocess
        path = str(self.grandchild).replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
                        f"$_.CommandLine -and $_.CommandLine.Contains('{path}') }} | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                       capture_output=True)

    def require_started(self):
        # A stop after a fixed deadline can come before both interpreters have started; then there
        # is nothing to observe, which is not a product failure.
        if not self.beat.exists():
            raise unittest.SkipTest("the grandchild had not started when the deadline passed")


@unittest.skipUnless(WINDOWS, "Job Objects are Windows-only")
class WindowsTreeStop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        background._TASKS.clear()
        self.dir = Path(tempfile.mkdtemp(dir=_tmp.name))
        self.ws = self.dir
        self.fx = Fixture(self.dir)

    async def asyncTearDown(self):
        for task in list(background._TASKS.values()):
            proc = task.get("proc")
            tree = proctree.tree_of(proc) if proc else None
            if tree:
                tree.terminate()
        background._TASKS.clear()
        self.fx.force_stop()

    async def test_kill_background_stops_the_grandchild(self):
        start = await tools.execute_tool("run_background", {"command": self.fx.command}, self.ws)
        self.assertTrue(start["ok"], start)
        tid = re.search(r"\[(bg-[^\]]+)\]", start["output"]).group(1)
        await self.fx.wait_started()
        killed = await tools.execute_tool("kill_background", {"id": tid}, self.ws)
        self.assertTrue(killed["ok"], killed)
        self.assertEqual(background._TASKS[tid]["status"], "killed")
        await self.fx.assert_beat_stops()

    async def test_kill_background_is_not_ok_while_the_tree_is_alive(self):
        start = await tools.execute_tool("run_background", {"command": self.fx.command}, self.ws)
        tid = re.search(r"\[(bg-[^\]]+)\]", start["output"]).group(1)
        await self.fx.wait_started()

        async def never_gone(self_, timeout=5.0):
            return False

        with patch.object(proctree.ProcessTree, "confirm_gone", never_gone):
            killed = await tools.execute_tool("kill_background", {"id": tid}, self.ws)
        self.assertFalse(killed["ok"], killed)
        self.assertIn("still running", killed["output"])
        self.assertTrue(background._TASKS[tid]["stop_unconfirmed"])
        # A retry signals the job again and, now that it can be confirmed, succeeds.
        retried = await tools.execute_tool("kill_background", {"id": tid}, self.ws)
        self.assertTrue(retried["ok"], retried)
        await self.fx.assert_beat_stops()

    async def test_foreground_deadline_stops_the_grandchild(self):
        started = time.monotonic()
        result = await tools._run_command_result(self.ws, {"command": self.fx.command, "timeout_sec": 2})
        self.assertTrue(result["timed_out"], result)
        self.assertLess(time.monotonic() - started, 8)
        self.fx.require_started()
        await self.fx.assert_beat_stops()

    async def test_foreground_cancel_stops_the_grandchild(self):
        task = asyncio.create_task(tools._run_command_result(self.ws, {"command": self.fx.command}))
        await self.fx.wait_started()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.fx.assert_beat_stops()

    async def test_hook_timeout_stops_the_grandchild(self):
        with patch.object(hooks, "TIMEOUT", 2.0):
            result = await hooks._run_cmd(self.fx.command, {}, self.ws)
        self.assertEqual(result["code"], 1, result)
        self.assertIn("timeout", result["err"])
        self.fx.require_started()
        await self.fx.assert_beat_stops()

    async def test_command_that_exits_on_its_own_keeps_detached_children(self):
        # Same as POSIX: only an explicit stop ends the tree, a normal exit does not.
        quick = self.dir / "quick.py"
        quick.write_text("import subprocess,sys\n"
                         "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],\n"
                         "    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n")
        cmd = f'"{sys.executable}" "{quick}" "{self.fx.grandchild}" "{self.fx.beat}"'
        result = await tools._run_command_result(self.ws, {"command": cmd, "timeout_sec": 10})
        self.assertTrue(result["ok"], result)
        await self.fx.wait_started()
        first = self.fx.beat.stat().st_size
        await asyncio.sleep(0.4)
        self.assertGreater(self.fx.beat.stat().st_size, first)

    async def test_command_that_cannot_be_contained_is_not_left_running(self):
        with patch.object(proctree, "adopt", return_value=None):
            with self.assertRaises(OSError):
                await proctree.spawn_shell(self.fx.command, cwd=str(self.dir),
                                           stdout=asyncio.subprocess.PIPE)
        # The suspended command was killed, so the grandchild never started.
        await asyncio.sleep(0.5)
        self.assertFalse(self.fx.beat.exists())

    async def test_unexpected_error_while_containing_does_not_leave_the_command_running(self):
        # A command that was never resumed cannot write a heartbeat, so look at the process itself.
        processes = []
        original = asyncio.create_subprocess_shell

        async def spawn(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            return proc

        with patch.object(proctree.asyncio, "create_subprocess_shell", side_effect=spawn),                 patch.object(proctree, "adopt", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await proctree.spawn_shell(self.fx.command, cwd=str(self.dir),
                                           stdout=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(processes[0].wait(), 5)  # killed, not left suspended
        finally:
            if processes[0].returncode is None:
                processes[0].kill()


class PortableSpawnOptions(unittest.TestCase):
    def test_posix_uses_a_new_session_and_windows_a_suspended_group(self):
        options = proctree.spawn_options()
        if os.name == "posix":
            self.assertEqual(options, {"start_new_session": True})
        elif WINDOWS:
            self.assertTrue(options["creationflags"] & 0x00000004)  # CREATE_SUSPENDED


if __name__ == "__main__":
    unittest.main()
