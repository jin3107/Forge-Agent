"""Prototype resolves an approved request to exact fresh bytes, without execution."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import hook_inspection
from engine.hook_review_requests import ReviewRequests
from engine.hook_snapshot import select_entries
from server import main


class AuthorizedSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name); (self.ws/'.forge').mkdir()
        self.path=self.ws/'.forge'/'config.json'
        self.raw=b'{"hooks":{"stop":"reviewed"}}'; self.path.write_bytes(self.raw)
        self.now=[100.0]; self.registry=ReviewRequests(ttl=10,clock=lambda:self.now[0])
        self.digest=hashlib.sha256(self.raw).hexdigest()
        self.row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
        self.assertEqual(self.registry.decide(self.row.id,'admin','approved',str(self.ws),self.digest,lambda _:True),'approved')
        self.admin=SimpleNamespace(enabled=True,role='admin')

    def resolve(self, owner='owner', sid='sid', ws=None):
        return main._approved_hook_snapshot(owner,sid,str(self.ws) if ws is None else ws)

    def patches(self):
        return patch.multiple(main,_hook_review_requests=self.registry,
                              _current_requested_workspace=lambda row:self.ws)

    def test_exact_approved_bytes_are_used_without_reopening_after_resolution(self):
        with self.patches(), patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            pair=self.resolve()
        self.assertIsNotNone(pair)
        decision,snapshot=pair
        self.path.write_bytes(b'{"hooks":{"stop":"unreviewed"}}')
        self.assertEqual(select_entries(decision,snapshot,'stop',{}),[{'command':'reviewed'}])
        with self.patches(), patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(self.resolve())
        self.path.write_bytes(self.raw)
        with self.patches(), patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(self.resolve(), 'observed stale approval must not revive')

    def test_scope_mismatch_expiry_and_revocation_reject(self):
        with self.patches(), patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
            self.assertIsNone(self.resolve(owner='other'))
            self.assertIsNone(self.resolve(sid='other'))
            self.assertIsNone(self.resolve(ws=str(self.ws/'other')))
            self.assertIsNotNone(self.resolve())
            self.now[0]+=11
            self.assertIsNone(self.resolve())
        self.assertIsNone(self.registry.get(self.row.id))

    def test_admin_disabled_or_demoted_invalidates(self):
        for role,enabled in [('member',True),('admin',False)]:
            with self.subTest(role=role,enabled=enabled):
                row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
                self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
                with self.patches(), patch.object(main.auth_web.auth_store,'get',return_value=SimpleNamespace(role=role,enabled=enabled)):
                    self.assertIsNone(self.resolve())
                self.assertIsNone(self.registry.get(row.id))

    def test_pending_denied_and_unshared_never_return_snapshot(self):
        self.registry.revoke(self.row.id,'owner',False)
        for share,status in [(True,'pending'),(True,'denied'),(False,'pending')]:
            row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=share)
            if status=='denied': self.registry.decide(row.id,'admin','denied',str(self.ws),self.digest,lambda _:True)
            with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
                self.assertIsNone(self.resolve())
            self.registry.revoke(row.id,'owner',False)

    def test_scope_changes_and_auth_failures_reject_without_leaking(self):
        for invalid in (None, self.ws/'other'):
            row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
            self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
            with patch.object(main,'_hook_review_requests',self.registry), patch.object(main,'_current_requested_workspace',return_value=invalid), patch.object(main.auth_web.auth_store,'get',return_value=self.admin):
                self.assertIsNone(self.resolve())
            self.assertIsNone(self.registry.get(row.id))
        row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
        self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
        with self.patches(),patch.object(main.auth_web.auth_store,'get',side_effect=OSError('secret')):
            self.assertIsNone(self.resolve())
        self.assertIsNone(self.registry.get(row.id))

    def test_admin_or_scope_changes_during_inspection_invalidate(self):
        for change in ('admin','workspace'):
            with self.subTest(change=change):
                self.registry=ReviewRequests(ttl=10,clock=lambda:self.now[0])
                row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
                self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
                admins=[self.admin,SimpleNamespace(enabled=False,role='admin') if change=='admin' else self.admin]
                workspaces=[self.ws,None if change=='workspace' else self.ws]
                with patch.object(main,'_hook_review_requests',self.registry), \
                     patch.object(main,'_current_requested_workspace',side_effect=workspaces), \
                     patch.object(main.auth_web.auth_store,'get',side_effect=admins):
                    self.assertIsNone(self.resolve())
                self.assertIsNone(self.registry.get(row.id))

    def test_invalid_raw_from_validator_never_releases_snapshot(self):
        for raw in (None,b'{',b'{"hooks":{"stop":"evil"}}',b'{"hooks":{"stop":"x"},"hooks":{}}'):
            with self.subTest(raw=raw):
                self.registry=ReviewRequests(ttl=10,clock=lambda:self.now[0])
                row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
                self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
                self.assertIsNone(self.registry.approved_snapshot('owner','sid',str(self.ws),lambda _:raw))
                self.assertIsNone(self.registry.get(row.id))

    def test_revocation_replacement_and_expiry_during_validation(self):
        for action in ('revoke','replace','expire'):
            self.registry=ReviewRequests(ttl=10,clock=lambda:self.now[0]); self.now[0]=100
            row=self.registry.submit('owner','sid',str(self.ws),self.digest,share_commands=True)
            self.registry.decide(row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
            real_inspect=hook_inspection.inspect
            def inspect(path):
                result=real_inspect(path)
                if action=='revoke': self.registry.revoke(row.id,'owner',False)
                if action=='replace': self.registry.submit('owner','sid',str(self.ws),'b'*64,share_commands=True)
                if action=='expire': self.now[0]+=11
                return result
            with self.patches(),patch.object(main.auth_web.auth_store,'get',return_value=self.admin), patch.object(hook_inspection,'inspect',side_effect=inspect):
                self.assertIsNone(self.resolve())
            if action=='replace': self.assertEqual(self.registry.list()[0].digest,'b'*64)
