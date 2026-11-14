"""Regression tests for tool authorization at the agent dispatch boundary.

Run with: python -m unittest discover -s tests -p 'test_tool_mode_enforcement.py'
No real provider, MCP server or credentials are used.
"""
import atexit
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-mode-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine.agent import Agent  # noqa: E402
from engine.session import SessionStore  # noqa: E402


def tool_call(name, args, call_id="call-test"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args),
    }}


class ToolModeEnforcement(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, mode, calls):
        store = SessionStore()
        store.allow_mcp = False
        session = store.create()
        agent = Agent(store, session.id)
        invocations = 0

        async def stream(messages, tools=None, model=None, temperature=0.3):
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                yield {"type": "tool_calls", "tool_calls": calls}
            else:
                yield {"type": "delta", "text": "Finished."}
            yield {"type": "finish", "reason": "stop"}

        agent._stream_chat = stream
        with patch("engine.hooks.pre_tool_use", new_callable=AsyncMock, return_value=None) as pre:
            events = [event async for event in agent.send("test task", chat_mode=mode)]
        return agent, store, events, pre

    async def test_disallowed_write_in_plan_and_direct(self):
        for mode in ("plan", "direct"):
            with self.subTest(mode=mode):
                agent, store, events, pre = await self.run_turn(
                    mode, [tool_call("write_file", {"path": "forbidden.txt", "content": "no"})])
                self.assertFalse((agent.workspace / "forbidden.txt").exists())
                result = next(e for e in events if e["type"] == "tool_result")
                self.assertFalse(result["ok"])
                self.assertIn("BLOCKED", result["output"])
                pre.assert_not_awaited()
                messages = store.get(agent.session.id).messages
                self.assertTrue(any(m.get("role") == "tool" and m.get("tool_call_id") == "call-test" for m in messages))
                self.assertTrue(any(e["type"] == "turn_end" for e in events))

    async def test_plan_allows_read_file(self):
        store = SessionStore()
        store.allow_mcp = False
        session = store.create()
        agent = Agent(store, session.id)
        (agent.workspace / "allowed.txt").write_text("readable marker", encoding="utf-8")
        count = 0

        async def stream(messages, tools=None, model=None, temperature=0.3):
            nonlocal count
            count += 1
            if count == 1:
                yield {"type": "tool_calls", "tool_calls": [tool_call("read_file", {"path": "allowed.txt"})]}
            else:
                yield {"type": "delta", "text": "Finished."}
            yield {"type": "finish", "reason": "stop"}

        agent._stream_chat = stream
        events = [e async for e in agent.send("read", chat_mode="plan")]
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "readable marker")

    async def test_unknown_and_unadvertised_mcp_denied_in_agent(self):
        calls = [tool_call("unknown_tool", {}, "a"), tool_call("mcp_fake_tool", {}, "b")]
        _, _, events, pre = await self.run_turn("agent", calls)
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not e["ok"] and "BLOCKED" in e["output"] for e in results))
        pre.assert_not_awaited()

    async def test_plan_rejects_parallel_spawn(self):
        calls = [tool_call("spawn_subagent", {"task": "x"}, "a"),
                 tool_call("spawn_subagent", {"task": "y"}, "b")]
        with patch.object(Agent, "_handle_parallel_spawns", side_effect=AssertionError("fan-out called")):
            _, _, events, pre = await self.run_turn("plan", calls)
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not e["ok"] for e in results))
        pre.assert_not_awaited()

    async def test_agent_allows_write_file(self):
        agent, _, events, pre = await self.run_turn(
            "agent", [tool_call("write_file", {"path": "allowed.txt", "content": "written"})])
        self.assertEqual((agent.workspace / "allowed.txt").read_text(encoding="utf-8"), "written")
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["ok"])
        pre.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
