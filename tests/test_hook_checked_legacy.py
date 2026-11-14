"""Prototype-only checked global legacy hook, real POSIX shell markers."""
import asyncio
import hashlib
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks
from engine.hook_approvals import HookApprovals


@unittest.skipUnless(os.name=='posix','shell commands tested on POSIX only')
class CheckedLegacy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.config=self.ws/'.forge'/'config.json'; self.registry=HookApprovals()
        self.config.write_text('{}')

    def resolve(self,owner,sid,ws): return self.registry.snapshot(ws,owner,sid)
    def cmd(self,marker,exitcode=0):
        script=(f"import os; from pathlib import Path; "
                f"Path({str(marker)!r}).write_text(os.environ.get('FORGE_CHANGED_FILE','missing')+'|'+os.environ.get('FORGE_WORKSPACE','missing')); "
                f"print('legacy output'); raise SystemExit({exitcode})")
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
    async def run_legacy(self,cfg,changed='out.txt',resolver=None):
        from engine.hook_checked_runner import checked_global_file_change
        return await checked_global_file_change(self.ws,'owner','sid',changed,resolver or self.resolve,cfg)
    def set_hooks(self):
        raw=json.dumps({'hooks':{'stop':'noop'}}).encode(); self.config.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    async def test_no_hooks_global_executes_with_legacy_env_and_output(self):
        marker=self.ws/'marker'
        result=await self.run_legacy({'on_file_change':self.cmd(marker,3)})
        self.assertEqual(marker.read_text(),f'out.txt|{self.ws}')
        self.assertEqual(result,'\n🔗 Hook on_file_change (exit 3): legacy output')

    async def test_pending_or_invalid_workspace_blocks_without_global_hook(self):
        marker=self.ws/'marker'; cfg={'on_file_change':self.cmd(marker)}
        self.set_hooks()
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg)
        self.config.write_text('{')
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg)
        self.assertFalse(marker.exists())

    async def test_approved_workspace_legacy_does_not_replace_global(self):
        workspace_marker=self.ws/'workspace'; global_marker=self.ws/'global'
        raw=json.dumps({'on_file_change':self.cmd(workspace_marker)}).encode(); self.config.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest(); self.registry.approve(self.ws,'owner','sid',digest)
        await self.run_legacy({'on_file_change':self.cmd(global_marker)})
        self.assertTrue(global_marker.exists()); self.assertFalse(workspace_marker.exists())

    async def test_empty_global_still_checks_scope(self):
        self.assertEqual(await self.run_legacy({}),'')
        self.set_hooks()
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy({})

    async def test_config_or_revocation_during_hook_raises_after_side_effect(self):
        digest=self.set_hooks(); self.registry.approve(self.ws,'owner','sid',digest)
        marker=self.ws/'marker'; release=self.ws/'release'
        script=(f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('started'); "
                f"p=Path({str(release)!r}); deadline=time.monotonic()+5; "
                "exec('while not p.exists() and time.monotonic() < deadline:\\n time.sleep(0.01)')")
        command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        task=asyncio.create_task(self.run_legacy({'on_file_change':command}))
        try:
            async def wait():
                while not marker.exists(): await asyncio.sleep(.01)
            await asyncio.wait_for(wait(),3)
            self.registry.revoke(self.ws,'owner','sid'); release.write_text('go')
            with self.assertRaises(hooks.HookTrustDenied): await asyncio.wait_for(task,5)
            self.assertTrue(marker.exists())
        finally:
            release.write_text('go')
            if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_bad_input_and_resolver_failure_fail_closed(self):
        marker=self.ws/'marker'; cfg={'on_file_change':self.cmd(marker)}
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg,changed=None)
        def broken(*_): raise RuntimeError('private')
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg,resolver=broken)
        self.assertFalse(marker.exists())

    async def test_global_command_change_after_execution_denies_without_undo(self):
        marker=self.ws/'marker'; cfg={'on_file_change':self.cmd(marker)}; count=[0]
        def resolver(owner,sid,ws):
            count[0]+=1
            if count[0]==3: cfg['on_file_change']='different'
            return self.resolve(owner,sid,ws)
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg,resolver=resolver)
        self.assertTrue(marker.exists())

    async def test_malformed_global_hook_and_subprocess_failure_are_not_exposed(self):
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy({'on_file_change':42})
        with patch('engine.hook_checked_runner.asyncio.create_subprocess_shell',side_effect=OSError('PRIVATE ERROR')):
            result=await self.run_legacy({'on_file_change':'anything'})
        self.assertIn('execution or cleanup unconfirmed',result); self.assertNotIn('PRIVATE ERROR',result)

    async def test_global_command_mutation_before_execution_denies(self):
        marker=self.ws/'marker'; cfg={'on_file_change':self.cmd(marker)}
        count=[0]
        def resolver(owner,sid,ws):
            count[0]+=1
            if count[0]==2: cfg['on_file_change']='different'
            return self.resolve(owner,sid,ws)
        with self.assertRaises(hooks.HookTrustDenied): await self.run_legacy(cfg,resolver=resolver)
        self.assertFalse(marker.exists())
