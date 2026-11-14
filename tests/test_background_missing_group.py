"""A missing process/group is not proof that a stop signal was delivered."""
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from engine import background, tools

def task(proc):
    return {'id':'bg-group','cmd':'fixture','proc':proc,'buf':bytearray(),'off':0,'base_offset':0,
            'status':'running','code':None,'started':time.time(),'killed':False}

@unittest.skipUnless(os.name == 'posix', 'POSIX group signalling')
class MissingBackgroundGroup(unittest.IsolatedAsyncioTestCase):
    async def test_missing_group_with_exited_leader_does_not_claim_kill(self):
        proc = MagicMock(pid=123456,returncode=0)
        proc.wait = AsyncMock(return_value=0)
        t = task(proc)
        with patch('engine.background.os.killpg', side_effect=ProcessLookupError()):
            output, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain)
        self.assertTrue(output.startswith('Error'), output)
        self.assertIn('group', output.lower())
        self.assertEqual(t['status'],'running')
        self.assertFalse(t['killed'])
        self.assertIsNone(t['code'])
        self.assertIn('group stop could not be confirmed', background.render(t, True).lower())
        proc.kill.assert_not_called()
        proc.wait.assert_not_awaited()

    async def test_missing_group_with_live_leader_falls_back_to_direct_signal(self):
        proc = MagicMock(pid=123456,returncode=None)
        proc.wait = AsyncMock(return_value=-9)
        t = task(proc)
        with patch('engine.background.os.killpg', side_effect=ProcessLookupError()):
            output, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain, 'direct signal cannot establish group cleanup')
        proc.kill.assert_called_once()
        self.assertEqual(t['status'],'killed')
        self.assertEqual(t['code'],-9)
        self.assertIn('chỉ gửi tín hiệu tới tiến trình trực tiếp',output)
        self.assertIn('group stop could not be confirmed', background.render(t, True).lower())

    async def test_missing_group_and_direct_process_disappears_is_uncertain(self):
        proc = MagicMock(pid=123456,returncode=None)
        proc.kill.side_effect = ProcessLookupError()
        proc.wait = AsyncMock(return_value=0)
        t = task(proc)
        with patch('engine.background.os.killpg', side_effect=ProcessLookupError()):
            output, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain)
        self.assertTrue(output.startswith('Error'), output)
        self.assertFalse(t['killed'])
        self.assertEqual(t['status'],'running')
        self.assertIsNone(t['code'])
        proc.wait.assert_not_awaited()

    async def test_tool_result_and_scoped_summary_preserve_warning(self):
        proc = MagicMock(pid=123456, returncode=0)
        proc.wait = AsyncMock(return_value=0)
        t = task(proc)
        ws = Path('/tmp')
        t['scope'] = background._scope(ws, owner_id='alice', session_id='s')
        background._TASKS[t['id']] = t
        try:
            with patch('engine.background.os.killpg', side_effect=ProcessLookupError()):
                result = await tools.execute_tool('kill_background', {'id':t['id']}, ws, owner_id='alice', session_id='s')
            self.assertFalse(result['ok'])
            self.assertIn('group stop could not be confirmed', result['output'].lower())
            checked = await tools.execute_tool('check_background', {'id':t['id']}, ws, owner_id='alice', session_id='s')
            self.assertTrue(checked['ok'])
            self.assertIn('group stop could not be confirmed', checked['output'].lower())
            self.assertIn('group stop could not be confirmed', background.summary(ws, owner_id='alice', session_id='s').lower())
            self.assertNotIn(t['id'], background.summary(ws, owner_id='bob', session_id='s'))
        finally:
            background._TASKS.pop(t['id'], None)

    async def test_non_posix_missing_process_is_uncertain(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.kill.side_effect = ProcessLookupError()
        proc.wait = AsyncMock(return_value=0)
        t = task(proc)
        with patch.object(background, 'os', SimpleNamespace(name='nt')):
            output, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain)
        self.assertTrue(output.startswith('Error'))
        self.assertFalse(t['killed'])
        self.assertEqual(t['status'], 'running')
        self.assertIn('group stop could not be confirmed', background.render(t, True).lower())
        proc.wait.assert_not_awaited()
