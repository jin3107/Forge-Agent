"""Tool ok must represent whether the requested background stop was confirmed."""
import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import background, tools


def task():
    proc = MagicMock(pid=123456, returncode=None)
    proc.wait = AsyncMock(return_value=-9)
    return {'id':'bg-stop-result','cmd':'fixture', 'proc':proc, 'buf':bytearray(), 'off':0,
            'base_offset':0, 'status':'running', 'code':None, 'started':time.time(),
            'killed':False, 'scope':background._scope(Path('/tmp'), 'alice', 'session')}


@unittest.skipUnless(os.name == 'posix', 'POSIX group signal mock')
class StopToolResult(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = task()
        background._TASKS[self.t['id']] = self.t

    async def asyncTearDown(self):
        background._TASKS.pop(self.t['id'], None)

    async def call(self, owner='alice', session='session'):
        return await tools.execute_tool('kill_background', {'id':self.t['id']}, Path('/tmp'), owner_id=owner, session_id=session)

    async def test_direct_fallback_and_repeated_stop_are_not_ok(self):
        with patch('engine.background.os.killpg', side_effect=ProcessLookupError()):
            result = await self.call()
        self.assertFalse(result['ok'], result)
        self.assertIn('group stop could not be confirmed', result['output'])
        self.assertEqual(self.t['code'], -9)
        self.t['proc'].kill.assert_called_once()
        repeated = await self.call()
        self.assertFalse(repeated['ok'], repeated)
        self.assertIn('group stop could not be confirmed', repeated['output'])

    async def test_confirmed_group_stop_and_previously_finished_task_ok(self):
        with patch('engine.background.os.killpg'):
            result = await self.call()
        self.assertTrue(result['ok'], result)
        self.assertTrue((await self.call())['ok'])

    async def test_wait_error_then_success_and_scope_denial(self):
        self.t['proc'].wait = AsyncMock(side_effect=[OSError('private'), -9])
        with patch('engine.background.os.killpg') as killpg:
            denied = await self.call(owner='bob')
            self.assertFalse(denied['ok'])
            killpg.assert_not_called()
            failure = await self.call()
            self.assertFalse(failure['ok'])
            self.assertNotIn('private', failure['output'])
            success = await self.call()
            self.assertTrue(success['ok'], success)
            killpg.assert_called_once()
