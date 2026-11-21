"""Direct text write APIs preserve supplied newline bytes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server.main import FileSaveBody, create_workspace_file, save_workspace_file

class WorkspaceNewlineWrite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-newline-write-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
    async def test_save_and_create_use_no_newline_translation(self):
        original_open = Path.open
        calls = []
        def spy(p, *args, **kwargs):
            calls.append((p, args, kwargs.copy()))
            return original_open(p, *args, **kwargs)
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap), patch.object(Path, 'open', spy):
            await save_workspace_file('s', FileSaveBody(path='saved.txt', content='a\r\nb\n'), user=None)
            await create_workspace_file('s', FileSaveBody(path='new.txt', content='a\rb\r'), user=None)
        for name in ('saved.txt','new.txt'):
            matching = [(args, kwargs) for p,args,kwargs in calls if p.name == name]
            self.assertEqual(len(matching),1)
            self.assertEqual(matching[0][1].get('newline'), '')
        self.assertEqual((self.ws/'saved.txt').read_bytes(), b'a\r\nb\n')
        self.assertEqual((self.ws/'new.txt').read_bytes(), b'a\rb\r')
