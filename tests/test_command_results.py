"""Regression tests for shell exit status and command-result metadata."""
import atexit
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-command-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import tools  # noqa: E402


def python_cmd(code):
    # Replacing the POSIX shell keeps timeout test cleanup independent of
    # the known, separately tracked process-tree termination limitation.
    prefix = "exec " if os.name != "nt" else ""
    return prefix + f'"{sys.executable}" -c "{code}"'


class CommandResults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = Path(_tmp.name) / self.id().replace(".", "_")
        self.ws.mkdir(parents=True, exist_ok=True)
        self.snapshot = patch("engine.checkpoints.snapshot", new_callable=AsyncMock)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    async def test_nonzero_exit_is_failure_even_with_output(self):
        result = await tools.execute_tool("run_command", {
            "command": python_cmd("import sys; print('output marker'); sys.exit(7)")}, self.ws)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["blocked"])
        self.assertFalse(result["truncated"])
        self.assertIn("output marker", result["output"])

    async def test_zero_exit_is_success(self):
        result = await tools.execute_tool("run_command", {
            "command": python_cmd("print('all good')")}, self.ws)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["blocked"])
        self.assertFalse(result["truncated"])

    async def test_timeout_is_not_success(self):
        result = await tools.execute_tool("run_command", {
            "command": python_cmd("import time; time.sleep(5)"), "timeout_sec": 1}, self.ws)
        self.assertFalse(result["ok"], result)
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["blocked"])

    async def test_guard_block_has_no_exit_code(self):
        with patch("engine.config.load_config", return_value={"approval_mode": "guarded"}):
            result = await tools.execute_tool("run_command", {"command": "rm -rf /"}, self.ws)
        self.assertFalse(result["ok"], result)
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["blocked"])
        self.assertFalse(result["timed_out"])

    async def test_output_truncation_metadata(self):
        result = await tools.execute_tool("run_command", {
            "command": python_cmd("print('x'*70000)")}, self.ws)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["truncated"])
        self.assertIn("truncated", result["output"])

    async def test_spawn_error_is_failure(self):
        with patch("engine.tools.asyncio.create_subprocess_shell", new_callable=AsyncMock,
                   side_effect=OSError("test spawn error")):
            result = await tools.execute_tool("run_command", {"command": "echo hello"}, self.ws)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["blocked"])

    async def test_agent_event_preserves_failure_metadata(self):
        import json
        from engine.agent import Agent
        from engine.session import SessionStore
        store = SessionStore()
        store.allow_mcp = False
        agent = Agent(store, store.create().id)
        count = 0

        async def fake_stream(messages, tools=None, model=None, temperature=0.3):
            nonlocal count
            count += 1
            if count == 1:
                yield {"type": "tool_calls", "tool_calls": [{"id": "failed-command", "type": "function",
                    "function": {"name": "run_command", "arguments": json.dumps({
                        "command": python_cmd("import sys; sys.exit(7)")})}}]}
            else:
                yield {"type": "delta", "text": "Command failed."}
            yield {"type": "finish", "reason": "stop"}

        agent._stream_chat = fake_stream
        with patch("engine.agent.log_tool") as audit:
            events = [event async for event in agent.send("run a command")]
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["checkpoint_status"], "failed")
        self.assertIn("Undo may not restore", result["output"])
        tool_message = next(m for m in agent.session.messages if m.get("tool_call_id") == "failed-command")
        self.assertIn("Undo may not restore", tool_message["content"])
        command_audit = next(c for c in audit.call_args_list if c.args[1] == "run_command")
        self.assertFalse(command_audit.args[3])

    async def test_terminal_endpoint_preserves_failure_metadata(self):
        from server.main import ExecBody, workspace_exec
        with patch("server.main._ws", return_value=self.ws):
            result = await workspace_exec("session", ExecBody(
                command=python_cmd("import sys; sys.exit(7)")), user=None)
        self.assertFalse(result["ok"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
