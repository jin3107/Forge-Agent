"""Session-start cache must not mix workspace or configured commands."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import hooks


class HookSessionCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.cleanup)
        self.root = Path(self.tmp.name)
        self.a = self.root/'a'; self.b = self.root/'b'
        self.a.mkdir(); self.b.mkdir()
        hooks._SS_CACHE.clear()

    async def cleanup(self):
        hooks._SS_CACHE.clear()
        self.tmp.cleanup()

    def config(self, ws, command):
        (ws/'.forge').mkdir(exist_ok=True)
        (ws/'.forge'/'config.json').write_text(json.dumps({'hooks': {'session_start':[{'command':command}]}}))

    async def test_workspace_and_config_changes_do_not_reuse_old_context(self):
        self.config(self.a,'first')
        self.config(self.b,'second')
        async def run(command, payload, workspace, env_extra=None):
            return {'code':0, 'out':command + '@' + workspace.name, 'err':''}
        with patch('engine.hooks.config.load_config', return_value={}), patch('engine.hooks._run_cmd', side_effect=run) as call:
            self.assertEqual(await hooks.session_start_cache(self.a,'same'), 'first@a')
            self.assertEqual(await hooks.session_start_cache(self.a,'same'), 'first@a')
            self.assertEqual(call.await_count, 1)
            self.assertEqual(await hooks.session_start_cache(self.b,'same'), 'second@b')
            self.config(self.b,'changed')
            self.assertEqual(await hooks.session_start_cache(self.b,'same'), 'changed@b')
            self.assertEqual(call.await_count, 3)

    async def test_global_hook_change_invalidates_cache(self):
        state = {'hooks':{'session_start':'first'}}
        async def run(command, *args, **kwargs):
            return {'code':0,'out':command,'err':''}
        with patch('engine.hooks.config.load_config', side_effect=lambda: state), patch('engine.hooks._run_cmd', side_effect=run):
            self.assertEqual(await hooks.session_start_cache(self.a,'same'), 'first')
            state['hooks']['session_start'] = 'second'
            self.assertEqual(await hooks.session_start_cache(self.a,'same'), 'second')

    async def test_late_previous_workspace_cannot_replace_newer_cache(self):
        self.config(self.a,'first'); self.config(self.b,'second')
        entered, release = asyncio.Event(), asyncio.Event()
        async def run(command, *args, **kwargs):
            if command == 'first':
                entered.set()
                await release.wait()
            return {'code':0,'out':command,'err':''}
        with patch('engine.hooks.config.load_config', return_value={}), patch('engine.hooks._run_cmd', side_effect=run) as calls:
            old = asyncio.create_task(hooks.session_start_cache(self.a,'same'))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                self.assertEqual(await hooks.session_start_cache(self.b,'same'), 'second')
                release.set()
                self.assertEqual(await old, 'first')
                self.assertEqual(await hooks.session_start_cache(self.b,'same'), 'second')
                self.assertEqual(calls.await_count,2, 'stale request must not replace newer cached context')
            finally:
                release.set()
                await asyncio.gather(old, return_exceptions=True)

    async def test_direct_session_start_remains_compatible(self):
        self.config(self.a,'hello')
        with patch('engine.hooks.config.load_config', return_value={}), patch('engine.hooks._run_cmd', new_callable=AsyncMock, return_value={'code':0,'out':'context','err':''}):
            self.assertEqual(await hooks.session_start(self.a,'sid'), 'context')
