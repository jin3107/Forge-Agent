"""Regression tests for foreground/background sandbox and approval policy.

Docker is mocked; these tests do not prove real container isolation.
"""
import atexit
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-policy-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import tools  # noqa: E402
from engine.agent import Agent  # noqa: E402
from engine.session import SessionStore  # noqa: E402


class ExecutionPolicy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = Path(_tmp.name) / self.id().replace(".", "_")
        self.ws.mkdir(parents=True, exist_ok=True)
        self.snapshot = patch("engine.checkpoints.snapshot", new_callable=AsyncMock)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    async def test_required_sandbox_missing_fails_closed(self):
        with patch("engine.config.load_config", return_value={"docker_sandbox": True}), \
             patch("engine.guard.DOCKER_AVAILABLE", False), \
             patch("engine.tools.asyncio.create_subprocess_shell", new_callable=AsyncMock,
                   side_effect=OSError("unexpected host shell")) as shell, \
             patch("engine.tools.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   side_effect=OSError("unexpected executable")) as exe:
            for name in ("run_command", "run_background"):
                with self.subTest(name=name):
                    result = await tools.execute_tool(name, {"command": "echo hello"}, self.ws)
                    self.assertFalse(result["ok"], result)
                    self.assertIn("sandbox", result["output"].lower())
            shell.assert_not_awaited()
            exe.assert_not_awaited()

    async def test_sandbox_is_used_by_both_paths(self):
        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.stdout.read = AsyncMock(side_effect=[b"ok", b""])
        fake_proc.wait.return_value = 0
        docker_args = ["docker", "run", "--network=none", "test-image", "sh", "-c", "echo hello"]
        with patch("engine.config.load_config", return_value={"docker_sandbox": True}), \
             patch("engine.guard.DOCKER_AVAILABLE", True), \
             patch("engine.guard.docker_command", return_value=docker_args) as wrap, \
             patch("engine.tools.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc) as exe, \
             patch("engine.background.start", new_callable=AsyncMock, return_value=("", "Error: test")) as start:
            fg = await tools.execute_tool("run_command", {"command": "echo hello"}, self.ws)
            self.assertTrue(fg["ok"], fg)
            exe.assert_awaited_once()
            self.assertEqual(exe.await_args.args, tuple(docker_args))
            await tools.execute_tool("run_background", {"command": "echo hello"}, self.ws)
            self.assertEqual(start.await_args.kwargs["exec_args"], docker_args)
            self.assertEqual(wrap.call_count, 2)

    async def test_docker_spawn_error_never_falls_back_to_host(self):
        with patch("engine.config.load_config", return_value={"docker_sandbox": True}), \
             patch("engine.guard.DOCKER_AVAILABLE", True), \
             patch("engine.tools.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   side_effect=OSError("docker unavailable")), \
             patch("engine.tools.asyncio.create_subprocess_shell", new_callable=AsyncMock) as shell:
            for name in ("run_command", "run_background"):
                result = await tools.execute_tool(name, {"command": "echo hello"}, self.ws)
                self.assertFalse(result["ok"], result)
            shell.assert_not_awaited()

    async def test_unapproved_warn_is_blocked_for_both(self):
        with patch("engine.config.load_config", return_value={"approval_mode": "ask"}), \
             patch("engine.guard.assess_command", return_value={"level": "warn", "reasons": ["test"]}), \
             patch("engine.tools.asyncio.create_subprocess_shell", new_callable=AsyncMock,
                   side_effect=OSError("unexpected host shell")) as shell:
            for name in ("run_command", "run_background"):
                result = await tools.execute_tool(name, {"command": "echo test"}, self.ws)
                self.assertFalse(result["ok"], result)
                self.assertIn("approval", result["output"].lower())
            shell.assert_not_awaited()

    async def test_background_approval_denial_never_runs(self):
        store = SessionStore()
        store.allow_mcp = False
        agent = Agent(store, store.create().id)
        count = 0
        async def stream(messages, tools=None, model=None, temperature=0.3):
            nonlocal count
            count += 1
            if count == 1:
                yield {"type": "tool_calls", "tool_calls": [{"id": "c", "type": "function",
                    "function": {"name": "run_background", "arguments": json.dumps({"command": "echo test"})}}]}
            else:
                yield {"type": "delta", "text": "denied"}
            yield {"type": "finish", "reason": "stop"}
        agent._stream_chat = stream
        with patch("engine.config.load_config", return_value={"approval_mode": "ask"}), \
             patch("engine.guard.assess_command", return_value={"level": "warn", "reasons": ["test"]}), \
             patch("engine.approvals.wait", new_callable=AsyncMock, return_value="deny"), \
             patch("engine.background.start", new_callable=AsyncMock) as start:
            events = [e async for e in agent.send("test")]
        self.assertTrue(any(e["type"] == "approval_request" for e in events))
        self.assertFalse(next(e for e in events if e["type"] == "tool_result")["ok"])
        start.assert_not_awaited()

    async def test_background_approval_is_forwarded(self):
        with patch("engine.config.load_config", return_value={"approval_mode": "ask"}), \
             patch("engine.guard.assess_command", return_value={"level": "danger", "reasons": ["test"]}), \
             patch("engine.background.start", new_callable=AsyncMock, return_value=("", "Error: test")) as start:
            await tools.execute_tool("run_background", {"command": "echo test"}, self.ws, override=True)
            start.assert_awaited_once()

    async def test_background_starts_docker_with_exec_not_host_shell(self):
        from engine import background
        fake_proc = AsyncMock()
        fake_proc.stdout.read = AsyncMock(return_value=b"")
        fake_proc.wait.return_value = 0
        argv = ["docker", "run", "image", "sh", "-c", "echo test"]
        with patch("engine.background.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=fake_proc) as exe, \
             patch("engine.background.asyncio.create_subprocess_shell", new_callable=AsyncMock) as shell:
            tid, _ = await background.start("echo test", self.ws, exec_args=argv)
            await asyncio.sleep(0)
            exe.assert_awaited_once()
            self.assertEqual(exe.await_args.args, tuple(argv))
            shell.assert_not_awaited()
            background._TASKS.pop(tid, None)

    async def test_docker_arguments_are_not_host_shell_interpolated(self):
        from engine.guard import docker_command
        command = "printf '%s' '$(echo not-executed-on-host)'"
        args = docker_command(command, self.ws)
        self.assertEqual(args[-1], command)
        self.assertIn("--network=none", args)
        self.assertIn("--read-only", args)
        self.assertIn(f"{self.ws.resolve()}:/work", args)

    async def test_approve_always_reused_for_background(self):
        store = SessionStore()
        store.allow_mcp = False
        agent = Agent(store, store.create().id)
        count = 0
        async def stream(messages, tools=None, model=None, temperature=0.3):
            nonlocal count
            count += 1
            if count == 1:
                yield {"type": "tool_calls", "tool_calls": [
                    {"id": str(i), "type": "function", "function": {"name": "run_background",
                     "arguments": json.dumps({"command": "echo test"})}} for i in (1, 2)]}
            else:
                yield {"type": "delta", "text": "done"}
            yield {"type": "finish", "reason": "stop"}
        agent._stream_chat = stream
        with patch("engine.config.load_config", return_value={"approval_mode": "ask"}), \
             patch("engine.guard.assess_command", return_value={"level": "warn", "reasons": ["test"]}), \
             patch("engine.approvals.wait", new_callable=AsyncMock, return_value="approve_always") as wait, \
             patch("engine.tools.execute_tool", new_callable=AsyncMock,
                   return_value={"ok": True, "output": "started"}) as execute:
            events = [event async for event in agent.send("test")]
        self.assertEqual(sum(e["type"] == "approval_request" for e in events), 1)
        wait.assert_awaited_once()
        self.assertEqual(execute.await_count, 2)
        self.assertTrue(all(call.kwargs["override"] for call in execute.await_args_list))


if __name__ == "__main__":
    unittest.main()
