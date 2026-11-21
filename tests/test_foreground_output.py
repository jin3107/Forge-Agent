"""Regression coverage for bounded foreground shell output."""
import atexit
import asyncio
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-fg-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import tools  # noqa: E402


def python_cmd(code):
    prefix = "exec " if os.name != "nt" else ""
    return prefix + f'"{sys.executable}" -u -c "{code}"'


class ForegroundOutput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = Path(_tmp.name) / self.id().replace(".", "_")
        self.ws.mkdir(parents=True, exist_ok=True)
        self.snap = patch("engine.checkpoints.snapshot", new_callable=AsyncMock)
        self.snap.start()
        self.addCleanup(self.snap.stop)

    async def run_command(self, code, **kw):
        return await tools.execute_tool("run_command", {"command": python_cmd(code), **kw}, self.ws)

    async def test_large_real_output_is_bounded_while_draining(self):
        tracemalloc.start()
        try:
            result = await self.run_command("import sys; sys.stdout.write('x'*8000000); sys.exit(7)", timeout_sec=15)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["truncated"])
        self.assertIn("truncated", result["output"])
        self.assertLess(peak, 4_000_000, f"Python allocation peak {peak} bytes; output was buffered")

    async def test_exact_cap_and_cap_plus_one(self):
        for extra in (0, 1):
            with self.subTest(extra=extra):
                result = await self.run_command(
                    f"import sys; sys.stdout.write('x'*{tools.MAX_CMD_OUTPUT + extra})")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["truncated"], bool(extra))
                self.assertEqual(result["exit_code"], 0)

    async def test_multibyte_cap_is_in_bytes(self):
        result = await self.run_command(
            "import sys; sys.stdout.buffer.write(bytes([195,169])*7000)")
        self.assertTrue(result["truncated"], result)
        self.assertLessEqual(result["output"].count('é'), tools.MAX_CMD_OUTPUT // 2)

    async def test_utf8_character_cut_at_cap_is_not_corrupted(self):
        code = f"import sys; sys.stdout.buffer.write(b'x'*{tools.MAX_CMD_OUTPUT - 1}+bytes([195,169])+b'Z')"
        result = await self.run_command(code)
        self.assertTrue(result["truncated"])
        self.assertNotIn("�", result["output"])

    async def test_stderr_is_captured_and_exit_code_preserved(self):
        result = await self.run_command("import sys; sys.stderr.write('stderr-marker'); sys.exit(3)")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("stderr-marker", result["output"])
        self.assertFalse(result["truncated"])

    async def test_cancellation_kills_and_reaps_direct_process(self):
        started = asyncio.Event()
        async def read(size):
            started.set()
            await asyncio.Event().wait()
        proc = unittest.mock.MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[read, b""])
        # AsyncMock side_effect functions in an iterable are returned, not called.
        async def read_once(size):
            proc.stdout.read.side_effect = [b""]
            await read(size)
        proc.stdout.read.side_effect = read_once
        proc.wait = AsyncMock(return_value=0)
        proc.pid = 123456
        if os.name == "posix":
            with patch("engine.tools.os.killpg") as killpg, \
                 patch("engine.tools.asyncio.create_subprocess_shell", AsyncMock(return_value=proc)):
                task = asyncio.create_task(tools._run_command_result(self.ws, {"command": "echo hello"}, override=True))
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            killpg.assert_called_once_with(proc.pid, tools.signal.SIGKILL)
        else:
            with patch("engine.tools.asyncio.create_subprocess_shell", AsyncMock(return_value=proc)):
                task = asyncio.create_task(tools._run_command_result(self.ws, {"command": "echo hello"}, override=True))
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    async def test_timeout_reports_partial_output_and_truncation(self):
        # The command has five seconds to start and write before the timeout; a one-second
        # budget was shorter than interpreter start-up on a slow Windows machine.
        code = "import sys,time; sys.stdout.write('x'*70000); sys.stdout.flush(); time.sleep(8)"
        result = await self.run_command(code, timeout_sec=5)
        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["truncated"])
        self.assertIn("partial", result["output"].lower())


if __name__ == "__main__":
    unittest.main()
