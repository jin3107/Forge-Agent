"""Real marker tests for standalone checked pre/post-tool prototypes."""
import hashlib
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import hooks
from engine.hook_review_requests import ReviewRequests
from server import main


@unittest.skipUnless(os.name=='posix','shell quoting tested on POSIX only')
class CheckedToolHooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.path=self.ws/'.forge'/'config.json'; self.registry=ReviewRequests()
        self.admin=SimpleNamespace(enabled=True,role='admin')

    def cmd(self,marker,exit_code=0):
        script=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit({exit_code})"
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    def config(self,obj,approve=False):
        raw=json.dumps(obj).encode(); self.path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        row=self.registry.submit('owner','sid',str(self.ws),digest,share_commands=True)
        if approve: self.registry.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True)
        return row

    def resolve(self,owner,sid,ws):
        return main._hook_scope_snapshot(owner,sid,str(ws))

    def patches(self):
        return patch.multiple(main,_hook_review_requests=self.registry,_current_requested_workspace=lambda _:self.ws)

    async def test_pending_pre_denial_blocks_consequential_tool_and_approved_exit2_blocks(self):
        from engine.hook_checked_runner import checked_pre_tool_use
        hook=self.ws/'hook'; tool=self.ws/'tool'
        row=self.config({'hooks':{'pre_tool_use':[{'command':self.cmd(hook,2),'matcher':'write_file'}]}})
        async def operation():
            pre=await checked_pre_tool_use(self.ws,'owner','sid','write_file',{'path':'tool'},self.resolve,{})
            if not pre: tool.write_text('tool ran')
            return pre
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            with self.assertRaises(hooks.HookTrustDenied): await operation()
            self.assertFalse(hook.exists()); self.assertFalse(tool.exists())
            self.registry.decide(row.id,'admin','approved',str(self.ws),row.digest,lambda _:True)
            result=await operation()
            self.assertTrue(result['blocked']); self.assertTrue(hook.exists()); self.assertFalse(tool.exists())

    async def test_matchers_and_absent_global_precedence(self):
        from engine.hook_checked_runner import checked_pre_tool_use
        a=self.ws/'a'; b=self.ws/'b'; g=self.ws/'global'
        self.config({'hooks':{'pre_tool_use':[{'command':self.cmd(a),'matcher':'^read'}, {'command':self.cmd(b),'matcher':'write'}]}},approve=True)
        global_config={'hooks':{'pre_tool_use':self.cmd(g)}}
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,global_config))
            self.assertFalse(a.exists()); self.assertTrue(b.exists()); self.assertFalse(g.exists())
            self.path.unlink()
            self.assertIsNone(await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,global_config))
            self.assertTrue(g.exists())

    async def test_revocation_after_first_pre_hook_blocks_second(self):
        from engine.hook_checked_runner import checked_pre_tool_use
        first=self.ws/'first'; second=self.ws/'second'
        row=self.config({'hooks':{'pre_tool_use':[self.cmd(first),self.cmd(second)]}},approve=True)
        original=hooks._run_cmd
        async def run_and_revoke(*args,**kwargs):
            out=await original(*args,**kwargs); self.registry.revoke(row.id,'owner',False); return out
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin),patch.object(hooks,'_run_cmd',side_effect=run_and_revoke):
            with self.assertRaises(hooks.HookTrustDenied):
                await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,{})
        self.assertTrue(first.exists()); self.assertFalse(second.exists())

    async def test_post_uses_matcher_and_skips_legacy(self):
        from engine.hook_checked_runner import checked_post_tool_use
        post=self.ws/'post'; legacy=self.ws/'legacy'; ignored=self.ws/'ignored'
        self.config({'hooks':{'post_tool_use':[{'command':self.cmd(post),'matcher':'write'}, {'command':self.cmd(ignored),'matcher':'read'}]},'on_file_change':self.cmd(legacy)},approve=True)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            await checked_post_tool_use(self.ws,'owner','sid','write_file',{},'result',self.resolve,{})
        self.assertTrue(post.exists()); self.assertFalse(legacy.exists()); self.assertFalse(ignored.exists())

    async def test_post_denial_does_not_undo_existing_tool_effect(self):
        from engine.hook_checked_runner import checked_post_tool_use
        tool=self.ws/'tool'; marker=self.ws/'post'
        self.config({'hooks':{'post_tool_use':self.cmd(marker)}})
        tool.write_text('already happened')
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            with self.assertRaises(hooks.HookTrustDenied):
                await checked_post_tool_use(self.ws,'owner','sid','write_file',{},'result',self.resolve,{})
        self.assertEqual(tool.read_text(),'already happened'); self.assertFalse(marker.exists())

    async def test_post_payload_truncation_and_env_are_compatible(self):
        from unittest.mock import AsyncMock
        from engine.hook_checked_runner import checked_post_tool_use
        self.config({'hooks':{'post_tool_use':'placeholder'}},approve=True)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin), \
             patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':3,'out':'out','err':'err'}) as run:
            message=await checked_post_tool_use(self.ws,'owner','sid','write_file',{'x':1},'a'*5000,self.resolve,{}, {'K':'V'})
        self.assertIn('exit 3',message); self.assertIn('err',message)
        args=run.await_args.args
        self.assertEqual(args[1]['tool_output'],'a'*4000)
        self.assertEqual(args[1]['tool_input'],{'x':1})
        self.assertEqual(args[1]['session_id'],'sid')
        self.assertEqual(args[3],{'K':'V'})

    async def test_post_revocation_between_hooks_stops_second(self):
        from engine.hook_checked_runner import checked_post_tool_use
        first=self.ws/'first'; second=self.ws/'second'
        row=self.config({'hooks':{'post_tool_use':[self.cmd(first),self.cmd(second)]}},approve=True)
        original=hooks._run_cmd
        async def run_and_revoke(*args,**kwargs):
            out=await original(*args,**kwargs); self.registry.revoke(row.id,'owner',False); return out
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin),patch.object(hooks,'_run_cmd',side_effect=run_and_revoke):
            with self.assertRaises(hooks.HookTrustDenied):
                await checked_post_tool_use(self.ws,'owner','sid','write_file',{},'already executed',self.resolve,{})
        self.assertTrue(first.exists()); self.assertFalse(second.exists())

    async def test_empty_match_does_not_bypass_unapproved_config(self):
        from engine.hook_checked_runner import checked_pre_tool_use
        self.config({'hooks':{'pre_tool_use':[{'command':self.cmd(self.ws/'x'),'matcher':'^other$'}]}})
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            with self.assertRaises(hooks.HookTrustDenied):
                await checked_pre_tool_use(self.ws,'owner','sid','write_file',{},self.resolve,{})
