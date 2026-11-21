"""Uploads must not overwrite existing files unless explicitly requested."""
import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import UploadFile
from server.main import upload_files

def file(name, data=b'new'):
    return UploadFile(filename=name, file=io.BytesIO(data))

class UploadNoOverwrite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-upload-no-clobber-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root/'ws'; self.ws.mkdir()
        (self.ws/'existing.bin').write_bytes(b'original\x00bytes')
    async def test_existing_skipped_without_checkpoint(self):
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            r = await upload_files('s', [file('existing.bin')], user=None)
        self.assertEqual(r['saved'], [])
        self.assertEqual(r['skipped'], ['existing.bin (already exists)'])
        self.assertNotIn('checkpoint_status', r)
        self.assertEqual((self.ws/'existing.bin').read_bytes(), b'original\x00bytes')
        snap.assert_not_awaited()
    async def test_new_duplicate_and_existing_in_one_request(self):
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap) as snapmock:
            r = await upload_files('s', [file('existing.bin'),file('new.txt',b'one'),file('new.txt',b'two')], user=None)
        self.assertEqual(r['saved'], ['new.txt'])
        self.assertEqual(r['skipped'], ['existing.bin (already exists)','new.txt (already exists)'])
        self.assertEqual((self.ws/'new.txt').read_bytes(),b'one')
        self.assertEqual(snapmock.await_count,1)
    async def test_explicit_overwrite_preserves_old_behavior(self):
        async def snap(*args, outcome=None, **kwargs):
            self.assertEqual((self.ws/'existing.bin').read_bytes(), b'original\x00bytes')
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            r = await upload_files('s', [file('existing.bin',b'replaced')], user=None, overwrite=True)
        self.assertEqual(r['saved'],['existing.bin'])
        self.assertEqual(r['checkpoint_status'],'created')
        self.assertEqual((self.ws/'existing.bin').read_bytes(), b'replaced')
    async def test_new_file_competes_immediately_before_open(self):
        target = self.ws/'race.txt'
        original = Path.open
        def raced(p, mode='r', *args, **kwargs):
            if p == target and mode == 'xb':
                fd = os.open(target, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
                with os.fdopen(fd,'wb') as out: out.write(b'winner')
            return original(p,mode,*args,**kwargs)
        async def snap(*args, outcome=None, **kwargs): outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap), patch.object(Path,'open',raced):
            r = await upload_files('s', [file('race.txt',b'overwrite')], user=None)
        self.assertEqual(r['saved'], [])
        self.assertEqual(r['skipped'], ['race.txt (already exists)'])
        self.assertEqual(target.read_bytes(), b'winner')
    async def test_two_competing_uploads_one_winner(self):
        count = 0; both = asyncio.Event()
        async def snap(*args, outcome=None, **kwargs):
            nonlocal count
            count += 1
            if count == 2: both.set()
            await asyncio.wait_for(both.wait(),2)
            outcome['status'] = 'created'
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', side_effect=snap):
            results = await asyncio.gather(upload_files('s',[file('same',b'one')],user=None),upload_files('s',[file('same',b'two')],user=None))
        self.assertEqual(sum(len(r['saved']) for r in results),1)
        self.assertEqual(sum(len(r['skipped']) for r in results),1)
        self.assertIn((self.ws/'same').read_bytes(),(b'one',b'two'))
    @unittest.skipUnless(os.name == 'posix', 'symlinks required')
    async def test_existing_symlinks_including_dangling_are_skipped(self):
        outside = self.root/'outside'; outside.write_bytes(b'safe')
        (self.ws/'external').symlink_to(outside)
        (self.ws/'dangling').symlink_to(self.ws/'missing')
        with patch('server.main._ws', return_value=self.ws), patch('engine.checkpoints.snapshot', new_callable=AsyncMock) as snap:
            r = await upload_files('s',[file('external'),file('dangling')],user=None)
        self.assertEqual(len(r['skipped']),2)
        self.assertEqual(outside.read_bytes(),b'safe')
        self.assertFalse((self.ws/'missing').exists())
        snap.assert_not_awaited()
