"""Undo restores the latest pre-operation snapshot, then moves the stack back."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import checkpoints, tools


class UndoSemantics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not checkpoints.available():
            self.skipTest('git unavailable')
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-undo-semantics-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.repo = self.root / 'ckpt.git'
        self.patches = [patch.object(checkpoints, '_repo_dir', return_value=self.repo),
                        patch('engine.config.load_config', return_value={'checkpoints_enabled': True})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def write(self, value):
        result = await tools.execute_tool('write_file', {'path': 'file.txt', 'content': value}, self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result.get('checkpoint_status'), 'created', result)

    async def test_first_operation_can_be_undone_to_preoperation_state(self):
        path = self.ws / 'file.txt'
        path.write_text('before')
        await self.write('after')
        result = await checkpoints.undo('fixture', self.ws)
        self.assertTrue(result['ok'], result)
        self.assertEqual(path.read_text(), 'before')
        self.assertEqual(await checkpoints.list_checkpoints('fixture', self.ws), [])
        self.assertFalse((await checkpoints.undo('fixture', self.ws))['ok'])
        await self.write('new-branch')
        self.assertTrue((await checkpoints.undo('fixture', self.ws))['ok'])
        self.assertEqual(path.read_text(), 'before')

    async def test_two_operations_undo_one_at_a_time(self):
        path = self.ws / 'file.txt'
        path.write_text('zero')
        await self.write('one')
        await self.write('two')
        first = await checkpoints.undo('fixture', self.ws)
        self.assertTrue(first['ok'], first)
        self.assertEqual(path.read_text(), 'one')
        second = await checkpoints.undo('fixture', self.ws)
        self.assertTrue(second['ok'], second)
        self.assertEqual(path.read_text(), 'zero')
        self.assertFalse((await checkpoints.undo('fixture', self.ws))['ok'])

    async def test_new_operation_after_undo_pushes_a_new_checkpoint(self):
        path = self.ws / 'file.txt'
        path.write_text('zero')
        await self.write('one')
        await self.write('two')
        self.assertTrue((await checkpoints.undo('fixture', self.ws))['ok'])
        self.assertEqual(path.read_text(), 'one')
        await self.write('alternative')
        self.assertTrue((await checkpoints.undo('fixture', self.ws))['ok'])
        self.assertEqual(path.read_text(), 'one')
        self.assertTrue((await checkpoints.undo('fixture', self.ws))['ok'])
        self.assertEqual(path.read_text(), 'zero')
        self.assertFalse((await checkpoints.undo('fixture', self.ws))['ok'])

    async def test_created_file_is_removed_by_undo_of_first_operation(self):
        await self.write('new')
        self.assertTrue((self.ws / 'file.txt').exists())
        result = await checkpoints.undo('fixture', self.ws)
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.ws / 'file.txt').exists())


if __name__ == '__main__':
    unittest.main()
