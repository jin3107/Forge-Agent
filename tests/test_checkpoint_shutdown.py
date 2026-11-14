"""Graceful shutdown waits for the snapshot of tracked Git cleanup tasks."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from engine import background, checkpoints


class CheckpointShutdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        background.resume_starts()

    async def asyncTearDown(self):
        background.resume_starts()

    async def test_empty_and_multiple_pending_wait_is_bounded(self):
        self.assertEqual(await checkpoints.wait_git_cleanups(), 0)
        first, second = asyncio.Event(), asyncio.Event()
        a = asyncio.create_task(first.wait())
        b = asyncio.create_task(second.wait())
        checkpoints._GIT_CLEANUPS.update((a, b))
        try:
            first.set()
            await a
            with patch.object(checkpoints, 'GIT_CLEANUP_SHUTDOWN_WAIT', .05, create=True):
                self.assertEqual(await checkpoints.wait_git_cleanups(), 1)
            self.assertFalse(b.done())
            self.assertFalse(b.cancelled())
        finally:
            second.set()
            await asyncio.gather(a, b, return_exceptions=True)
            checkpoints._GIT_CLEANUPS.difference_update((a, b))

    async def test_wait_only_includes_tasks_present_at_start(self):
        first, later = asyncio.Event(), asyncio.Event()
        a = asyncio.create_task(first.wait())
        b = asyncio.create_task(later.wait())
        checkpoints._GIT_CLEANUPS.add(a)
        waiter = asyncio.create_task(checkpoints.wait_git_cleanups())
        try:
            await asyncio.sleep(0)
            checkpoints._GIT_CLEANUPS.add(b)
            first.set()
            self.assertEqual(await asyncio.wait_for(waiter, 1), 0)
            self.assertFalse(b.done())
        finally:
            first.set()
            later.set()
            await asyncio.gather(a, b, waiter, return_exceptions=True)
            checkpoints._GIT_CLEANUPS.difference_update((a, b))

    async def test_lifespan_waits_concurrently_for_checkpoint_cleanup(self):
        from server.main import app
        release, background_started = asyncio.Event(), asyncio.Event()
        cleanup = asyncio.create_task(release.wait())
        checkpoints._GIT_CLEANUPS.add(cleanup)
        async def shutdown():
            background_started.set()
            return {'pending_starts': 0, 'failures': []}
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        exit_task = None
        try:
            with patch('server.main.background.shutdown', side_effect=shutdown):
                exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
                await asyncio.wait_for(background_started.wait(), 2)
                await asyncio.sleep(.03)
                self.assertFalse(exit_task.done())
                release.set()
                await asyncio.wait_for(exit_task, 2)
        finally:
            release.set()
            await cleanup
            checkpoints._GIT_CLEANUPS.discard(cleanup)
            if exit_task is None:
                await cm.__aexit__(None, None, None)
            elif not exit_task.done():
                await asyncio.wait_for(exit_task, 2)

    async def test_lifespan_timeout_warns_without_cancelling(self):
        from server.main import app
        release = asyncio.Event()
        cleanup = asyncio.create_task(release.wait())
        checkpoints._GIT_CLEANUPS.add(cleanup)
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        try:
            with patch.object(checkpoints, 'GIT_CLEANUP_SHUTDOWN_WAIT', .05, create=True), \
                 self.assertLogs('server.main', level='WARNING') as logs:
                await asyncio.wait_for(cm.__aexit__(None, None, None), 2)
            self.assertIn('Checkpoint', ' '.join(logs.output))
            self.assertFalse(cleanup.done())
            self.assertFalse(cleanup.cancelled())
        finally:
            release.set()
            await cleanup
            checkpoints._GIT_CLEANUPS.discard(cleanup)

    async def test_other_shutdown_failure_does_not_skip_checkpoint_wait(self):
        from server.main import app
        release = asyncio.Event()
        cleanup = asyncio.create_task(release.wait())
        checkpoints._GIT_CLEANUPS.add(cleanup)
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        exit_task = None
        try:
            with patch('server.main.background.shutdown', new_callable=AsyncMock, side_effect=RuntimeError('fixture')), \
                 self.assertLogs('server.main', level='WARNING') as logs:
                exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
                await asyncio.sleep(.05)
                self.assertFalse(exit_task.done())
                release.set()
                await asyncio.wait_for(exit_task, 2)
            self.assertIn('Background shutdown failed', ' '.join(logs.output))
        finally:
            release.set()
            await cleanup
            checkpoints._GIT_CLEANUPS.discard(cleanup)
            if exit_task is None:
                await cm.__aexit__(None, None, None)
            elif not exit_task.done():
                await asyncio.wait_for(exit_task, 2)

    async def test_checkpoint_wait_failure_is_logged(self):
        from server.main import app
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        with patch('engine.checkpoints.wait_git_cleanups', new_callable=AsyncMock,
                   side_effect=RuntimeError('fixture'), create=True), \
             self.assertLogs('server.main', level='WARNING') as logs:
            await cm.__aexit__(None, None, None)
        self.assertIn('Checkpoint cleanup wait failed', ' '.join(logs.output))


if __name__ == '__main__':
    unittest.main()
