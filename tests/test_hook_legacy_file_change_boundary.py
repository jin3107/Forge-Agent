"""Characterize the real legacy tool hook; these tests do NOT enable a gate.

Only POSIX subprocesses and a mocked checkpoint/config store are exercised.
"""
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from engine import config, hooks, tools
from engine.hook_approvals import HookApprovals
from engine.hook_checked_runner import checked_pre_tool_use, checked_post_tool_use


@unittest.skipUnless(os.name == 'posix', 'shell quoting tested only on POSIX')
class LegacyFileChangeBoundary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.path = self.ws/'.forge'/'config.json'
        self.registry = HookApprovals()

    def cmd(self, marker, exit_code=0):
        script = (f"import os; from pathlib import Path; "
                  f"Path({str(marker)!r}).write_text(os.environ.get('FORGE_CHANGED_FILE','missing')+'|'+os.environ.get('FORGE_WORKSPACE','missing')); "
                  f"raise SystemExit({exit_code})")
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    def resolve(self, owner, sid, ws):
        return self.registry.snapshot(ws, owner, sid)

    async def run_tool(self, name, args, global_config):
        with patch('engine.checkpoints.snapshot', new_callable=AsyncMock), \
             patch('engine.lsp.available_for', return_value=False), \
             patch.object(config, 'load_config', return_value=global_config):
            return await tools.execute_tool(name, args, self.ws, owner_id='owner', session_id='sid')

    async def test_direct_write_runs_global_legacy_even_with_pending_workspace_hooks(self):
        workspace_marker = self.ws/'workspace-marker'; global_marker = self.ws/'global-marker'
        self.path.write_text(json.dumps({'on_file_change': self.cmd(workspace_marker)}))
        self.assertEqual(self.resolve('owner','sid',self.ws)[0].state, 'approval_required')
        cfg = {'on_file_change': self.cmd(global_marker)}
        with self.assertRaises(hooks.HookTrustDenied):
            await checked_pre_tool_use(self.ws,'owner','sid','write_file',{'path':'out.txt'},self.resolve,cfg)
        self.assertFalse((self.ws/'out.txt').exists())
        # execute_tool is a separate entry point; a checked pre-call is not enforced here.
        result = await self.run_tool('write_file', {'path':'out.txt','content':'value'}, cfg)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.ws/'out.txt').read_text(), 'value')
        self.assertFalse(workspace_marker.exists())
        self.assertEqual(global_marker.read_text(), f'out.txt|{self.ws}')
        self.assertIn('Hook on_file_change (exit 0)',result['output'])

    async def test_global_legacy_runs_even_after_workspace_legacy_approved(self):
        workspace_marker=self.ws/'workspace-marker'; global_marker=self.ws/'global-marker'
        self.path.write_text(json.dumps({'on_file_change':self.cmd(workspace_marker)}))
        decision=self.registry.check(self.ws,'owner','sid')
        self.assertEqual(self.registry.approve(self.ws,'owner','sid',decision.digest).state,'approved')
        cfg={'on_file_change':self.cmd(global_marker)}
        self.assertIsNone(await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,cfg))
        result=await self.run_tool('write_file',{'path':'out.txt','content':'one'},cfg)
        await checked_post_tool_use(self.ws,'owner','sid','write_file',{},result['output'],self.resolve,cfg)
        self.assertTrue(global_marker.exists()); self.assertFalse(workspace_marker.exists())

    async def test_legacy_nonzero_exit_does_not_undo_write_or_fail_tool(self):
        marker=self.ws/'legacy'; cfg={'on_file_change':self.cmd(marker,3)}
        result=await self.run_tool('write_file',{'path':'out.txt','content':'value'},cfg)
        self.assertTrue(result['ok']); self.assertTrue(marker.exists())
        self.assertEqual((self.ws/'out.txt').read_text(),'value')
        self.assertIn('exit 3',result['output'])

    async def test_edit_success_runs_legacy_but_failed_edit_and_read_do_not(self):
        marker=self.ws/'legacy'; cfg={'on_file_change':self.cmd(marker)}
        (self.ws/'out.txt').write_text('old')
        failed=await self.run_tool('edit_file',{'path':'out.txt','old_text':'missing','new_text':'new'},cfg)
        self.assertFalse(failed['ok']); self.assertFalse(marker.exists())
        read=await self.run_tool('read_file',{'path':'out.txt'},cfg)
        self.assertTrue(read['ok']); self.assertFalse(marker.exists())
        changed=await self.run_tool('edit_file',{'path':'out.txt','old_text':'old','new_text':'new'},cfg)
        self.assertTrue(changed['ok']); self.assertEqual(marker.read_text(),f'out.txt|{self.ws}')
        self.assertEqual((self.ws/'out.txt').read_text(),'new')

    async def test_change_after_checked_pre_allows_global_hook_before_checked_post_denies(self):
        self.path.write_text('{}')
        marker=self.ws/'legacy'; cfg={'on_file_change':self.cmd(marker)}
        self.assertIsNone(await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,cfg))
        # The check is complete; the real tool has its own unguarded legacy hook.
        self.path.write_text(json.dumps({'hooks':{'stop':'unapproved'}}))
        result=await self.run_tool('write_file',{'path':'out.txt','content':'already written'},cfg)
        with self.assertRaises(hooks.HookTrustDenied):
            await checked_post_tool_use(self.ws,'owner','sid','write_file',{},result['output'],self.resolve,cfg)
        self.assertTrue(result['ok']); self.assertTrue(marker.exists())
        self.assertEqual((self.ws/'out.txt').read_text(),'already written')

    async def test_malformed_workspace_does_not_stop_direct_global_legacy(self):
        self.path.write_text('{')
        marker=self.ws/'legacy'; cfg={'on_file_change':self.cmd(marker)}
        with self.assertRaises(hooks.HookTrustDenied):
            await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,cfg)
        result=await self.run_tool('write_file',{'path':'out.txt','content':'value'},cfg)
        self.assertTrue(result['ok']); self.assertTrue(marker.exists())
