"""Background task admission under concurrent asynchronous starts."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine import background


class BackgroundAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-admission-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.old_tasks = background._TASKS.copy()
        background._TASKS.clear()
        self.addCleanup(self.restore_tasks)

    def restore_tasks(self):
        background._TASKS.clear()
        background._TASKS.update(self.old_tasks)

    async def test_simultaneous_start_reserves_slots_before_spawn(self):
        entered = 0
        first_entered = asyncio.Event()
        release = asyncio.Event()
        proc = MagicMock()
        async def spawn(*args, **kwargs):
            nonlocal entered
            entered += 1
            first_entered.set()
            await release.wait()
            return proc
        with patch.object(background, 'MAX_RUNNING', 2), \
             patch('engine.background.asyncio.create_subprocess_shell', side_effect=spawn), \
             patch('engine.background._pump', new_callable=AsyncMock):
            tasks = [asyncio.create_task(background.start('test', self.ws, owner_id=f'user{i}'))
                     for i in range(10)]
            await asyncio.wait_for(first_entered.wait(), 2)
            await asyncio.sleep(0.05)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        accepted = [r for r in results if r[0]]
        rejected = [r for r in results if not r[0]]
        self.assertEqual(len(accepted), 2, results)
        self.assertEqual(len(rejected), 8, results)
        self.assertEqual(entered, 2)
        for _, msg in rejected:
            self.assertTrue(msg.startswith('Error'))
            self.assertNotIn('bg-', msg)

    async def test_spawn_failure_releases_capacity(self):
        with patch.object(background, 'MAX_RUNNING', 1), \
             patch('engine.background.asyncio.create_subprocess_shell', new_callable=AsyncMock,
                   side_effect=[OSError('spawn failed'), MagicMock()]), \
             patch('engine.background._pump', new_callable=AsyncMock):
            with self.assertRaises(OSError):
                await background.start('first', self.ws)
            tid, _ = await background.start('second', self.ws)
            self.assertTrue(tid)

    async def test_cancelled_spawn_releases_capacity_after_creation_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError('fixture spawn failed')
        with patch.object(background, 'MAX_RUNNING', 1), \
             patch('engine.background.asyncio.create_subprocess_shell', side_effect=spawn):
            task = asyncio.create_task(background.start('first', self.ws))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(background._STARTING, 1)
            release.set()
            async def settled():
                while background._STARTING:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(settled(), 2)
        with patch.object(background, 'MAX_RUNNING', 1), \
             patch('engine.background.asyncio.create_subprocess_shell', new_callable=AsyncMock, return_value=MagicMock()), \
             patch('engine.background._pump', new_callable=AsyncMock):
            tid, _ = await background.start('second', self.ws)
            self.assertTrue(tid)

    async def test_running_plus_inflight_are_both_counted(self):
        background._TASKS['existing'] = {'status': 'running'}
        entered = asyncio.Event()
        release = asyncio.Event()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return MagicMock()
        with patch.object(background, 'MAX_RUNNING', 2), \
             patch('engine.background.asyncio.create_subprocess_shell', side_effect=spawn) as mocked_spawn, \
             patch('engine.background._pump', new_callable=AsyncMock):
            task = asyncio.create_task(background.start('first', self.ws))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                tid, message = await background.start('second', self.ws)
                self.assertFalse(tid)
                self.assertTrue(message.startswith('Error'))
                self.assertEqual(mocked_spawn.await_count, 1)
            finally:
                release.set()
                await task

    async def test_real_subprocesses_respect_limit(self):
        tids = []
        try:
            with patch.object(background, 'MAX_RUNNING', 2):
                results = await asyncio.gather(*(
                    background.start('real child', self.ws,
                        exec_args=[sys.executable, '-u', '-c', 'import time; time.sleep(10)'])
                    for _ in range(8)))
                tids = [tid for tid, _ in results if tid]
                self.assertEqual(len(tids), 2, results)
                self.assertEqual(sum(bool(tid) for tid, _ in results), 2)
        finally:
            for tid in tids:
                await background.kill(tid, self.ws)

    async def test_running_slot_blocks_and_finished_slot_is_reusable(self):
        proc = MagicMock()
        with patch.object(background, 'MAX_RUNNING', 1), \
             patch('engine.background.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=proc) as spawn, \
             patch('engine.background._pump', new_callable=AsyncMock):
            first, _ = await background.start('first', self.ws, exec_args=['test'])
            second, message = await background.start('second', self.ws, exec_args=['test'])
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertTrue(message.startswith('Error'))
            background._TASKS[first]['status'] = 'done'
            third, _ = await background.start('third', self.ws, exec_args=['test'])
            self.assertTrue(third)
            self.assertEqual(spawn.await_count, 2)


if __name__ == '__main__':
    unittest.main()
