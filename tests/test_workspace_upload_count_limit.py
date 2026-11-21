"""Every file beyond the upload processing limit is reported as skipped."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import UploadFile
from server.main import upload_files

class WatchedFile(UploadFile):
    def __init__(self, name):
        super().__init__(filename=name, file=io.BytesIO(b'contents'))
        self.reads = 0
    async def read(self, size=-1):
        self.reads += 1
        return await super().read(size)

class UploadCountLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-upload-count-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
    async def test_twenty_are_processed_and_extras_reported_without_read(self):
        files = [WatchedFile(f'file-{i}.txt') for i in range(1,24)]
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as checkpoint:
            r = await upload_files('sid', files, user=None)
        self.assertEqual(len(r['saved']), 20)
        self.assertEqual(r['skipped'], [f'#{i}: file-{i}.txt (over 20-file limit)' for i in range(21,24)])
        self.assertEqual([f.reads for f in files], [1]*20 + [0]*3)
        self.assertTrue((self.ws/'file-20.txt').exists())
        self.assertFalse((self.ws/'file-21.txt').exists())
        self.assertEqual(checkpoint.await_count, 1)
        self.assertEqual(r['checkpoint_status'], 'created')
    async def test_twenty_or_fewer_have_no_count_skips(self):
        files = [WatchedFile(f'{i}.txt') for i in range(20)]
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'failed'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            r = await upload_files('sid',files,user=None)
        self.assertEqual(len(r['saved']),20)
        self.assertEqual(r['skipped'],[])
        self.assertEqual(r['checkpoint_status'],'failed')
        self.assertIn('undo',r['warning'].lower())
    async def test_first_twenty_all_skipped_no_checkpoint(self):
        files = [WatchedFile(f'.hidden-{i}') for i in range(20)] + [WatchedFile('ignored.txt')]
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot') as snap:
            r = await upload_files('sid',files,user=None)
        self.assertEqual(r['saved'],[])
        self.assertEqual(len(r['skipped']),21)
        self.assertEqual(r['skipped'][-1],'#21: ignored.txt (over 20-file limit)')
        self.assertNotIn('checkpoint_status',r)
        self.assertEqual(files[-1].reads,0)
        snap.assert_not_called()
    async def test_extra_names_are_bounded_and_indexed(self):
        files = [WatchedFile('.hidden') for _ in range(20)] + [WatchedFile('x'*500), WatchedFile('x'*500), WatchedFile('')]
        with patch('server.main._ws', return_value=self.ws):
            r = await upload_files('sid',files,user=None)
        self.assertEqual(r['skipped'][-3], '#21: ' + 'x'*200 + '… (over 20-file limit)')
        self.assertEqual(r['skipped'][-2], '#22: ' + 'x'*200 + '… (over 20-file limit)')
        self.assertEqual(r['skipped'][-1], '#23: ? (over 20-file limit)')
        self.assertTrue(all(f.reads == 0 for f in files))
