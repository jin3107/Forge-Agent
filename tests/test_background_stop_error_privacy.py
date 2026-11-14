"""Stop failures should not expose arbitrary OS exception details to tools."""
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from engine import background, tools


def make_task(proc):
    return {'id': 'bg-private-stop', 'cmd': 'fixture', 'proc': proc,
            'buf': bytearray(), 'off': 0, 'base_offset': 0,
            'status': 'running', 'code': None, 'started': time.time(),
            'killed': False, 'scope': background._scope(Path('/tmp'), 'alice', 's')}


class StopErrorPrivacy(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX group signalling')
    async def test_group_and_direct_error_details_not_returned_by_tool(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.kill.side_effect = PermissionError('direct-secret-path')
        proc.wait = AsyncMock(return_value=-9)
        t = make_task(proc)
        background._TASKS[t['id']] = t
        try:
            with patch('engine.background.os.killpg', side_effect=PermissionError('group-secret-path')):
                result = await tools.execute_tool('kill_background', {'id': t['id']}, Path('/tmp'), owner_id='alice', session_id='s')
            self.assertFalse(result['ok'])
            self.assertNotIn('secret-path', result['output'])
            self.assertIn('Error', result['output'])
            self.assertTrue(t['stop_unconfirmed'])
            self.assertEqual(t['status'], 'running')
            self.assertFalse(t['killed'])
            self.assertIn('stop could not be confirmed', background.render(t, True))
            proc.wait.assert_not_awaited()
        finally:
            background._TASKS.pop(t['id'], None)

    async def test_non_posix_error_details_not_returned(self):
        proc = MagicMock(pid=123456, returncode=None)
        proc.kill.side_effect = PermissionError('direct-secret-path')
        proc.wait = AsyncMock(return_value=-9)
        t = make_task(proc)
        with patch.object(background, 'os', SimpleNamespace(name='nt')):
            output, uncertain = await background._kill_task(t)
        self.assertTrue(uncertain)
        self.assertTrue(output.startswith('Error'))
        self.assertNotIn('direct-secret-path', output)
        self.assertTrue(t['stop_unconfirmed'])
        self.assertFalse(t['killed'])
        proc.wait.assert_not_awaited()
