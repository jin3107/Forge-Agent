"""Opt-in legacy hook runner at the actual write/edit boundary."""
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import hooks, tools

class InjectedLegacyTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.ws=Path(self.tmp.name).resolve()
    async def call(self,name,args,runner):
        with patch('engine.checkpoints.snapshot',new_callable=AsyncMock), patch('engine.lsp.available_for',return_value=False):
            return await tools.execute_tool(name,args,self.ws,owner_id='owner',session_id='sid',legacy_hook=runner)
    @unittest.skipUnless(os.name=='posix','POSIX shell quoting')
    async def test_real_checked_global_hook_on_valid_scope(self):
        from engine.hook_approvals import Decision, Snapshot
        from server.hook_runner import ScopedCheckedHookRunner
        marker=self.ws/'marker'
        script=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        runner=ScopedCheckedHookRunner(owner_id='owner',resolve=lambda *_:(Decision('absent'),Snapshot('absent',None,None)),
                                      load_config=lambda:{'on_file_change':cmd})
        result=await self.call('write_file',{'path':'file','content':'written'},runner.global_file_change)
        self.assertTrue(result['ok']); self.assertFalse(result.get('hook_trust_denied',False))
        self.assertEqual(marker.read_text(),'ran'); self.assertIn('Hook on_file_change',result['output'])

    async def test_denial_after_successful_write_preserves_result_and_effect(self):
        hook=AsyncMock(side_effect=hooks.HookTrustDenied())
        result=await self.call('write_file',{'path':'file','content':'written'},hook)
        self.assertTrue(result['ok']); self.assertTrue(result['hook_trust_denied'])
        self.assertIn('not rolled back',result['output']); self.assertEqual((self.ws/'file').read_text(),'written')
        hook.assert_awaited_once_with(self.ws,'sid','file')
    async def test_adapter_errors_and_bad_result_fail_closed_without_leaking_details(self):
        for outcome in (OSError('private'),None):
            hook=AsyncMock(side_effect=outcome) if isinstance(outcome,Exception) else AsyncMock(return_value=outcome)
            result=await self.call('write_file',{'path':'file','content':'written'},hook)
            self.assertTrue(result['hook_trust_denied']); self.assertNotIn('private',result['output'])
    async def test_valid_adapter_result_and_failed_or_read_tools(self):
        hook=AsyncMock(return_value='\nchecked legacy ran')
        result=await self.call('write_file',{'path':'file','content':'written'},hook)
        self.assertTrue(result['ok']); self.assertIn('checked legacy ran',result['output']); self.assertFalse(result.get('hook_trust_denied',False))
        hook.reset_mock()
        await self.call('read_file',{'path':'file'},hook)
        await self.call('edit_file',{'path':'file','old_text':'missing','new_text':'new'},hook)
        hook.assert_not_awaited()
