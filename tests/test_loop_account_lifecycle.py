"""A scheduled loop follows the life of its account and its session, and administrators can see it.

In-process tests of the scheduler and of the server's loop turn: no model, no network.
"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-loop-life-")
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import scheduler  # noqa: E402
from server import main  # noqa: E402


def owner(enabled=True, email="member@example.test"):
    return SimpleNamespace(id="owner-1", email=email, enabled=enabled)


class LoopTurnChecksTheAccount(unittest.IsolatedAsyncioTestCase):
    """The check is the first thing a tick does, so it covers demo and non-demo ticks alike."""

    async def tick(self, found, credentials, session=True):
        built = []
        scoped = SimpleNamespace(get=lambda sid: SimpleNamespace(model="test") if session else None)
        with patch.object(main.auth_web.auth_store, "get", side_effect=found), \
                patch.object(main, "ScopedSessionStore", return_value=scoped), \
                patch.object(main, "Agent", side_effect=lambda *a, **k: built.append(k)), \
                patch.object(main.config, "has_provider_credentials", return_value=credentials):
            try:
                await main._loop_turn("s", "work", "agent", "owner-1", False)
                error = None
            except scheduler.LoopStop as exc:
                error = exc
        return error, built

    async def test_disabled_owner_ends_the_loop_for_demo_and_non_demo_ticks(self):
        for credentials in (False, True):
            with self.subTest(demo=not credentials):
                error, built = await self.tick(lambda _id: owner(enabled=False), credentials)
                self.assertEqual(error.code, "owner_disabled")
                self.assertIn("member@example.test", error.message)
                self.assertEqual(built, [])  # no agent was built, so no turn ran

    async def test_missing_owner_ends_the_loop(self):
        error, built = await self.tick(lambda _id: None, False)
        self.assertEqual(error.code, "owner_missing")
        self.assertIn("owner-1", error.message)
        self.assertEqual(built, [])

    async def test_a_failed_account_lookup_fails_closed(self):
        def broken(_id):
            raise RuntimeError("database is locked")
        error, built = await self.tick(broken, False)
        self.assertEqual(error.code, "owner_check_failed")
        self.assertNotIn("database", error.message)  # no internal detail
        self.assertEqual(built, [])

    async def test_deleted_session_ends_the_loop(self):
        error, _ = await self.tick(lambda _id: owner(), False, session=False)
        self.assertEqual(error.code, "session_deleted")

    async def test_enabled_owner_with_a_session_runs(self):
        # Agent is stubbed to return None, so the turn fails after the checks: only that they passed matters.
        with patch.object(main.auth_web.auth_store, "get", return_value=owner()), \
                patch.object(main, "ScopedSessionStore",
                             return_value=SimpleNamespace(get=lambda sid: SimpleNamespace(model="test"))), \
                patch.object(main, "Agent", return_value=SimpleNamespace(send=lambda *a, **k: _empty())), \
                patch.object(main, "send_demo", lambda agent, text: _empty()), \
                patch.object(main.config, "has_provider_credentials", return_value=False):
            self.assertEqual(await main._loop_turn("s", "work", "agent", "owner-1", False), "")


async def _empty():
    return
    yield {}


class SchedulerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_loop_stop_ends_the_loop_and_is_reported_with_its_code(self):
        manager, ticks = scheduler.LoopManager(), []

        async def run(sid, text, mode):
            ticks.append(sid)
            raise scheduler.LoopStop("session_deleted", "its session is gone")

        manager.start("s", 0, "work", "agent", run)
        await asyncio.wait_for(manager._loops["s"]["task"], 1)
        status = manager.status("s")
        self.assertEqual(ticks, ["s"])  # not woken again
        self.assertFalse(status["running"])
        self.assertTrue(status["finished"])
        self.assertIs(status["last_ok"], False)
        self.assertEqual(status["last_error_code"], "session_deleted")
        self.assertEqual(status["last_error"], "its session is gone")

    async def test_stop_owner_ends_only_that_accounts_loops_and_keeps_the_reason(self):
        manager, ticks = scheduler.LoopManager(), []

        async def run(sid, text, mode):
            ticks.append(sid)

        manager.start("a1", 3600, "work", "agent", run, owner_id="alice")
        manager.start("a2", 3600, "work", "agent", run, owner_id="alice")
        manager.start("b1", 3600, "work", "agent", run, owner_id="bob")
        await asyncio.sleep(0.05)
        self.assertEqual(manager.stop_owner("alice", "owner_disabled", "Loop stopped: alice disabled"), 2)
        await asyncio.sleep(0.05)
        for sid in ("a1", "a2"):
            status = manager.status(sid)
            self.assertFalse(status["running"], status)
            self.assertEqual(status["last_error_code"], "owner_disabled")
        self.assertTrue(manager.status("b1")["running"])
        self.assertEqual(manager.stop_owner("alice", "owner_disabled", "again"), 0)  # nothing left to stop
        manager.stop("b1")

    async def test_status_all_lists_running_loops_with_owner_and_without_task_text(self):
        manager = scheduler.LoopManager()

        async def run(sid, text, mode):
            pass

        manager.start("mine", 3600, "private task text", "agent", run, owner_id="alice")
        manager.start("done", 3600, "other", "agent", run, max_runs=1, owner_id="bob")
        await asyncio.sleep(0.05)
        rows = manager.status_all()
        self.assertEqual([r["session"] for r in rows], ["mine"])  # a finished loop is not listed
        self.assertEqual(rows[0]["owner"], "alice")
        self.assertNotIn("text", rows[0])
        self.assertNotIn("private task text", str(rows))
        manager.stop("mine")


if __name__ == "__main__":
    unittest.main()
