"""No-replace publication must never overwrite another destination."""
import errno
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import worktree


def payload():
    out=io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        info=tarfile.TarInfo('file'); info.size=4; tar.addfile(info,io.BytesIO(b'data'))
    return out.getvalue()

class AtomicPublish(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.dst=self.root/'target'

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux renameat2')
    def test_real_linux_publish_without_copy(self):
        probe=self.root/'probe'; probe.mkdir(); renamed=self.root/'probe-renamed'
        if not worktree._rename_noreplace(probe,renamed):
            self.skipTest('filesystem or libc does not support renameat2')
        renamed.rmdir()
        with patch.object(worktree.shutil,'copytree',side_effect=AssertionError('must rename')):
            self.assertTrue(worktree._extract_archive_staged(payload(),self.dst))
        self.assertEqual((self.dst/'file').read_text(),'data')
        self.assertEqual([p.name for p in self.root.iterdir()],['target'])

    def test_destination_created_during_publish_is_preserved(self):
        def race(src,dst):
            dst.mkdir(); (dst/'sentinel').write_text('keep')
            raise FileExistsError(errno.EEXIST,'exists')
        with patch.object(worktree,'_rename_noreplace',side_effect=race), patch.object(worktree.shutil,'copytree') as copy:
            self.assertFalse(worktree._extract_archive_staged(payload(),self.dst))
        copy.assert_not_called(); self.assertEqual((self.dst/'sentinel').read_text(),'keep')
        self.assertFalse(any(p.name.startswith('.forge-archive-') for p in self.root.iterdir()))

    def test_unsupported_fallback_copies(self):
        with patch.object(worktree,'_rename_noreplace',return_value=False):
            self.assertTrue(worktree._extract_archive_staged(payload(),self.dst))
        self.assertEqual((self.dst/'file').read_text(),'data')

    def test_publish_failure_does_not_fallback_or_leave_destination(self):
        with patch.object(worktree,'_rename_noreplace',side_effect=OSError(errno.EACCES,'private')), patch.object(worktree.shutil,'copytree') as copy:
            self.assertFalse(worktree._extract_archive_staged(payload(),self.dst))
        copy.assert_not_called(); self.assertFalse(self.dst.exists())
        self.assertFalse(any(p.name.startswith('.forge-archive-') for p in self.root.iterdir()))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux renameat2')
    def test_kernel_noreplace_rejects_dangling_symlink(self):
        src=self.root/'src'; src.mkdir(); self.dst.symlink_to('missing')
        try:
            result=worktree._rename_noreplace(src,self.dst)
        except FileExistsError:
            pass
        else:
            if result is False: self.skipTest('filesystem does not support renameat2')
            self.fail('symlink replaced')
        self.assertTrue(src.exists()); self.assertTrue(self.dst.is_symlink())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux renameat2')
    def test_kernel_noreplace_rejects_existing_empty_directory(self):
        src=self.root/'src'; src.mkdir(); (src/'file').write_text('data'); self.dst.mkdir()
        try:
            result=worktree._rename_noreplace(src,self.dst)
        except FileExistsError:
            pass
        else:
            if result is False: self.skipTest('filesystem does not support renameat2')
            self.fail('existing empty directory replaced')
        self.assertTrue(src.exists()); self.assertEqual(list(self.dst.iterdir()),[])
