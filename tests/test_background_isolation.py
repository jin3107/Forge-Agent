"""Background task ownership regressions. No provider or external network required."""
import atexit
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory(prefix="forge-bg-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import background, tools  # noqa: E402
from engine.agent import Agent  # noqa: E402
from engine.session import ScopedSessionStore, SessionStore  # noqa: E402


class BackgroundIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        background._TASKS.clear()
        self.ws = Path(_tmp.name) / "workspace"
        self.ws.mkdir(exist_ok=True)

    async def asyncTearDown(self):
        for task in list(background._TASKS.values()):
            proc = task.get("proc")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
        background._TASKS.clear()

    async def test_scoped_list_read_and_kill(self):
        # A long-lived task lets us test both output access and kill authorization.
        prefix = "exec " if os.name != "nt" else ""
        cmd = prefix + f'"{sys.executable}" -u -c "import time; print(\'PRIVATE_MARKER\', flush=True); time.sleep(15)"'
        own = {"owner_id": "alice", "session_id": "alice-session"}
        other_owner = {"owner_id": "bob", "session_id": "bob-session"}
        other_session = {"owner_id": "alice", "session_id": "other-session"}
        start = await tools.execute_tool("run_background", {"command": cmd}, self.ws, **own)
        self.assertTrue(start["ok"], start)
        tid = re.search(r"\[(bg-[^\]]+)\]", start["output"]).group(1)
        for scope in (other_owner, other_session):
            listed = await tools.execute_tool("check_background", {}, self.ws, **scope)
            self.assertNotIn(tid, listed["output"])
            self.assertNotIn("PRIVATE_MARKER", listed["output"])
            read = await tools.execute_tool("check_background", {"id": tid}, self.ws, **scope)
            self.assertFalse(read["ok"])
            self.assertNotIn("PRIVATE_MARKER", read["output"])
            killed = await tools.execute_tool("kill_background", {"id": tid}, self.ws, **scope)
            self.assertFalse(killed["ok"])
            self.assertIsNone(background._TASKS[tid]["proc"].returncode)
        listed = await tools.execute_tool("check_background", {}, self.ws, **own)
        self.assertIn(tid, listed["output"])
        read = await tools.execute_tool("check_background", {"id": tid}, self.ws, **own)
        self.assertTrue(read["ok"])
        # Initial output may already have been consumed by run_background.
        self.assertIn(tid, read["output"])
        killed = await tools.execute_tool("kill_background", {"id": tid}, self.ws, **own)
        self.assertTrue(killed["ok"])

    async def test_agent_passes_its_session_scope(self):
        import json
        base = SessionStore()
        alice_store = ScopedSessionStore(base, "alice", allow_mcp=False)
        bob_store = ScopedSessionStore(base, "bob", allow_mcp=False)
        alice = Agent(alice_store, alice_store.create().id)
        bob = Agent(bob_store, bob_store.create().id)
        prefix = "exec " if os.name != "nt" else ""
        command = prefix + f'"{sys.executable}" -u -c "import time; print(\'PRIVATE_MARKER\', flush=True); time.sleep(15)"'

        async def turn(agent, name, args):
            n = 0
            async def stream(messages, tools=None, model=None, temperature=0.3):
                nonlocal n
                n += 1
                if n == 1:
                    yield {"type": "tool_calls", "tool_calls": [{"id": "c", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}
                else:
                    yield {"type": "delta", "text": "done"}
                yield {"type": "finish", "reason": "stop"}
            agent._stream_chat = stream
            events = [e async for e in agent.send("test", chat_mode="agent")]
            return next(e for e in events if e["type"] == "tool_result")

        started = await turn(alice, "run_background", {"command": command})
        self.assertTrue(started["ok"], started)
        tid = re.search(r"\[(bg-[^\]]+)\]", started["output"]).group(1)
        for name, args in (("check_background", {}), ("check_background", {"id": tid}),
                           ("kill_background", {"id": tid})):
            result = await turn(bob, name, args)
            self.assertNotIn("PRIVATE_MARKER", result["output"])
            self.assertNotIn(tid, result["output"] if not args else "")
            if args:
                self.assertFalse(result["ok"])
        result = await turn(alice, "kill_background", {"id": tid})
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
