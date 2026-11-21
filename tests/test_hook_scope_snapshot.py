"""Prototype scope resolution for absent, hook-free and approved configs."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import hook_inspection
from engine.hook_review_requests import ReviewRequests
from engine.hook_snapshot import select_entries
from server import main


class ScopeSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.path=self.ws/'.forge'/'config.json'
        self.registry=ReviewRequests(); self.admin=SimpleNamespace(enabled=True,role='admin')

    def resolve(self):
        return main._hook_scope_snapshot('owner','sid',str(self.ws))

    def patches(self):
        return patch.multiple(main,_hook_review_requests=self.registry,
                              _current_requested_workspace=lambda row:self.ws)

    def approve(self):
        raw=b'{"hooks":{"stop":"reviewed"}}'; self.path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        row=self.registry.submit('owner','sid',str(self.ws),digest,share_commands=True)
        self.registry.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True)
        return row,raw

    def test_absent_and_no_hooks_use_global_without_request(self):
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            for raw,state in [(None,'absent'),(b'{}','no_hooks'),(b'{"hooks":{}}','no_hooks')]:
                if raw is None: self.path.unlink(missing_ok=True)
                else: self.path.write_bytes(raw)
                decision,snapshot=self.resolve()
                self.assertEqual(decision.state,state)
                self.assertEqual(select_entries(decision,snapshot,'stop',{'hooks':{'stop':'global'}}),[{'command':'global'}])

    def test_observed_absent_no_hooks_invalidates_old_approval(self):
        for raw in (None,b'{}'):
            with self.subTest(raw=raw):
                row,original=self.approve()
                if raw is None: self.path.unlink()
                else: self.path.write_bytes(raw)
                with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
                    self.assertIn(self.resolve()[0].state,('absent','no_hooks'))
                    self.assertIsNone(self.registry.get(row.id))
                    self.path.write_bytes(original)
                    self.assertIsNone(self.resolve())

    def test_hooks_require_matching_approved_request_and_admin(self):
        row,raw=self.approve()
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertEqual(self.resolve()[0].state,'approved')
            self.registry.revoke(row.id,'owner',False)
            self.assertIsNone(self.resolve())
            pending=self.registry.submit('owner','sid',str(self.ws),hashlib.sha256(raw).hexdigest(),share_commands=True)
            self.assertIsNone(self.resolve())
            self.registry.decide(pending.id,'admin','approved',str(self.ws),pending.digest,lambda _:True)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=SimpleNamespace(enabled=False,role='admin')):
            self.assertIsNone(self.resolve())
        self.assertIsNone(self.registry.get(pending.id))

    def test_invalid_scope_or_invalid_config_never_falls_back_global(self):
        row,_=self.approve()
        self.path.write_bytes(b'{')
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(self.resolve())
        self.assertIsNone(self.registry.get(row.id))
        self.path.write_bytes(b'{}')
        with patch.object(main,'_hook_review_requests',self.registry),patch.object(main,'_current_requested_workspace',return_value=None):
            self.assertIsNone(self.resolve())

    def test_owner_workspace_switch_invalidates_old_approval(self):
        row,raw=self.approve()
        other=self.ws/'other'; other.mkdir()
        with patch.object(main,'_hook_review_requests',self.registry),patch.object(main,'_current_requested_workspace',return_value=other):
            pair=main._hook_scope_snapshot('owner','sid',str(other))
        self.assertEqual(pair[0].state,'absent')
        self.assertIsNone(self.registry.get(row.id))
        self.path.write_bytes(raw)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(self.resolve())

    def test_inconsistent_scope_inspections_never_authorize_fallback(self):
        bad=[hook_inspection.Inspection('absent','x',None,None),
             hook_inspection.Inspection('no_hooks','0'*64,{},b'{}'),
             hook_inspection.Inspection('no_hooks',hashlib.sha256(b'{"hooks":{"stop":"x"}}').hexdigest(),{},b'{"hooks":{"stop":"x"}}')]
        for result in bad:
            with self.subTest(result=result):
                self.assertIsNone(self.registry.scope_snapshot('owner','sid',str(self.ws),lambda:result,lambda _:True))

    def test_admin_reentrant_revocation_or_replacement_rejects(self):
        for action in ('revoke','replace'):
            with self.subTest(action=action):
                self.registry=ReviewRequests(); row,raw=self.approve()
                def validate_admin(_):
                    if action=='revoke': self.registry.revoke(row.id,'owner',False)
                    else: self.registry.submit('owner','sid',str(self.ws),'b'*64,share_commands=True)
                    return True
                result=self.registry.scope_snapshot('owner','sid',str(self.ws),lambda:hook_inspection.inspect(self.ws),validate_admin)
                self.assertIsNone(result)
                if action=='replace': self.assertEqual(self.registry.list()[0].digest,'b'*64)

    def test_replacement_during_scope_validation_is_not_deleted(self):
        row,raw=self.approve(); real=hook_inspection.inspect
        def inspect(ws):
            result=real(ws)
            self.registry.submit('owner','sid',str(self.ws),'b'*64,share_commands=True)
            return result
        with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin),patch.object(hook_inspection,'inspect',side_effect=inspect):
            self.assertIsNone(self.resolve())
        self.assertEqual(self.registry.list()[0].digest,'b'*64)
