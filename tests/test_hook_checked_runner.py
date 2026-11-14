"""Real marker-command tests for the standalone checked-event prototype.

Not wired to the production hook runner; not an authorization boundary.
"""
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
from engine.hook_approvals import Decision, Snapshot
from engine.hook_review_requests import ReviewRequests
from server import main


@unittest.skipUnless(os.name=='posix','shell quoting tested on POSIX only')
class CheckedEventRunner(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.config=self.ws/'.forge'/'config.json'
        self.registry=ReviewRequests()
        self.admin=SimpleNamespace(enabled=True,role='admin')

    def command(self, marker):
        script=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'

    def write_config(self,commands):
        raw=json.dumps({'hooks':{'user_prompt_submit':commands}}).encode()
        self.config.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def submit(self,digest,approve=False):
        row=self.registry.submit('owner','sid',str(self.ws),digest,share_commands=True)
        if approve:
            self.assertEqual(self.registry.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True),'approved')
        return row

    def resolve(self,owner,sid,workspace):
        return main._approved_hook_snapshot(owner,sid,str(workspace))

    async def run_event(self, resolver=None):
        from engine.hook_checked_runner import run_checked_event
        return await run_checked_event(self.ws,'owner','sid','user_prompt_submit',{},resolver or self.resolve,{})

    async def test_unapproved_approval_revocation_and_changed_config(self):
        marker=self.ws/'marker'
        digest=self.write_config([self.command(marker)])
        row=self.submit(digest)
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            with self.assertRaises(hooks.HookTrustDenied): await self.run_event()
            self.assertFalse(marker.exists())
            self.registry.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True)
            result=await self.run_event()
            self.assertEqual(result[0]['rc'],0)
            self.assertTrue(marker.exists())
            marker.unlink()
            self.write_config([self.command(self.ws/'changed')])
            with self.assertRaises(hooks.HookTrustDenied): await self.run_event()
            self.assertFalse((self.ws/'changed').exists())
            self.assertIsNone(self.registry.get(row.id))
            self.write_config([self.command(marker)])
            with self.assertRaises(hooks.HookTrustDenied): await self.run_event()
            self.assertFalse(marker.exists())

    async def test_selected_bytes_not_reopened_after_resolver_returns(self):
        marker=self.ws/'approved'; changed=self.ws/'unapproved'
        digest=self.write_config([self.command(marker)]); self.submit(digest,approve=True)
        def resolve_and_replace(owner,sid,ws):
            pair=self.resolve(owner,sid,ws)
            self.write_config([self.command(changed)])
            return pair
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            with self.assertRaises(hooks.HookTrustDenied):
                await self.run_event(resolve_and_replace)
        self.assertTrue(marker.exists()); self.assertFalse(changed.exists())

    async def test_revocation_after_only_command_reports_denial_but_effect_remains(self):
        marker=self.ws/'first'
        digest=self.write_config([self.command(marker)])
        row=self.submit(digest,approve=True)
        original=hooks._run_cmd
        async def execute_then_revoke(*args,**kwargs):
            result=await original(*args,**kwargs)
            self.registry.revoke(row.id,'owner',False)
            return result
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin), \
             patch.object(hooks,'_run_cmd',side_effect=execute_then_revoke):
            with self.assertRaises(hooks.HookTrustDenied): await self.run_event()
        self.assertTrue(marker.exists())

    async def test_revocation_between_commands_blocks_later_command(self):
        first=self.ws/'first'; second=self.ws/'second'
        digest=self.write_config([self.command(first),self.command(second)])
        row=self.submit(digest,approve=True)
        original=hooks._run_cmd
        async def execute_then_revoke(*args,**kwargs):
            result=await original(*args,**kwargs)
            self.registry.revoke(row.id,'owner',False)
            return result
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin), \
             patch.object(hooks,'_run_cmd',side_effect=execute_then_revoke):
            with self.assertRaises(hooks.HookTrustDenied): await self.run_event()
        self.assertTrue(first.exists()); self.assertFalse(second.exists())

    async def test_bad_resolver_and_nonapproved_snapshot_fail_closed(self):
        for resolver in (lambda *_:None, lambda *_:(_ for _ in ()).throw(OSError('private')),
                         lambda *_:(Decision('no_hooks','a'*64),Snapshot('no_hooks','a'*64,b'{}'))):
            with self.subTest(resolver=resolver),patch.object(hooks,'_run_cmd',side_effect=AssertionError('must not run')):
                with self.assertRaises(hooks.HookTrustDenied): await self.run_event(resolver)

    async def test_all_general_events_use_bound_scope_and_override_global(self):
        from unittest.mock import AsyncMock
        from engine.hook_checked_runner import run_checked_event
        raw=json.dumps({'hooks':{'stop':'noop','unknown':[]}}).encode()
        self.config.write_bytes(raw); digest=hashlib.sha256(raw).hexdigest(); self.submit(digest,approve=True)
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin), \
             patch.object(hooks,'_run_cmd',new_callable=AsyncMock,return_value={'code':0,'out':'ok','err':''}) as run:
            for event in ('user_prompt_submit','stop','pre_compact','notification'):
                result=await run_checked_event(self.ws,'owner','sid',event,
                         {'session_id':'forged','workspace':'forged','hook_event_name':'forged'},
                         self.resolve,{'hooks':{event:'global'}})
                self.assertEqual(len(result),int(event=='stop'))
            run.assert_awaited_once()
            payload=run.await_args.args[1]
            self.assertEqual(payload['session_id'],'sid')
            self.assertEqual(payload['workspace'],str(self.ws))
            self.assertEqual(payload['hook_event_name'],'stop')

    async def test_absent_and_no_hooks_use_global_but_invalid_or_pending_do_not(self):
        from engine.hook_checked_runner import run_checked_event
        global_marker=self.ws/'global'
        global_config={'hooks':{'stop':self.command(global_marker)}}
        def resolver(owner,sid,workspace):
            return main._hook_scope_snapshot(owner,sid,str(workspace))
        async def run():
            return await run_checked_event(self.ws,'owner','sid','stop',{},resolver,global_config)
        with patch.object(main,'_hook_review_requests',self.registry), \
             patch.object(main,'_current_requested_workspace',return_value=self.ws), \
             patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            for raw in (None,b'{}'):
                if raw is None: self.config.unlink(missing_ok=True)
                else: self.config.write_bytes(raw)
                result=await run()
                self.assertEqual(result[0]['rc'],0)
                self.assertTrue(global_marker.exists()); global_marker.unlink()
            self.config.write_bytes(b'{')
            with self.assertRaises(hooks.HookTrustDenied): await run()
            self.assertFalse(global_marker.exists())
            digest=self.write_config([self.command(self.ws/'workspace')]); self.submit(digest)
            with self.assertRaises(hooks.HookTrustDenied): await run()
            self.assertFalse(global_marker.exists())

    async def test_only_general_events_supported(self):
        from engine.hook_checked_runner import run_checked_event
        with patch.object(hooks,'_run_cmd',side_effect=AssertionError('must not run')):
            with self.assertRaises(hooks.HookTrustDenied):
                await run_checked_event(self.ws,'owner','sid','pre_tool_use',{},self.resolve,{})
