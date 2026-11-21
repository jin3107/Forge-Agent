"""Opt-in inherited resolver prototype, never the production hook gate."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import config, hook_inspection, hook_lineage
from engine.hook_review_requests import ReviewRequests
from engine.session import SessionStore, ScopedSessionStore
from server import main

class InheritedResolver(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); (root/'sessions').mkdir(); (root/'workspaces').mkdir()
        p=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.base=SessionStore(); self.scoped=ScopedSessionStore(self.base,'owner')
        self.parent=self.scoped.create(model='test'); self.child=self.scoped.create(model='test')
        self.ws=self.scoped.workspace(self.parent.id).resolve(); self.scoped.open_workspace(self.child.id,str(self.ws))
        (self.ws/'.forge').mkdir(); self.cfg=self.ws/'.forge'/'config.json'; self.cfg.write_text(json.dumps({'hooks':{'stop':'echo ok'}}))
        self.digest=hook_inspection.inspect(self.ws).digest; self.review=ReviewRequests()
        self.row=self.review.submit('owner',self.parent.id,str(self.ws),self.digest,share_commands=True)
        self.review.decide(self.row.id,'admin','approved',str(self.ws),self.digest,lambda _:True)
        for p in [patch.object(main,'store',self.base),patch.object(main,'_hook_review_requests',self.review),
                  patch.object(main.auth_web.auth_store,'get',side_effect=lambda who:SimpleNamespace(enabled=True,role='admin' if who=='admin' else 'member'))]:
            p.start(); self.addCleanup(p.stop)
    def resolve(self): return main._hook_scope_snapshot_with_inheritance('owner',self.child.id,str(self.ws))
    def test_inheritance_only_while_active_and_revoke(self):
        self.assertIsNone(self.resolve())
        with hook_lineage.track_shared_child(self.scoped,self.parent.id,self.child.id,self.ws) as active:
            self.assertTrue(active); self.assertEqual(self.resolve()[0].state,'approved')
            self.assertTrue(self.review.revoke(self.row.id,'owner',False)); self.assertIsNone(self.resolve())
        self.assertIsNone(self.resolve())
    def test_explicit_child_pending_or_denial_not_overridden(self):
        with hook_lineage.track_shared_child(self.scoped,self.parent.id,self.child.id,self.ws):
            row=self.review.submit('owner',self.child.id,str(self.ws),self.digest,share_commands=True)
            self.assertIsNone(self.resolve())
            self.assertEqual(self.review.decide(row.id,'admin','denied',str(self.ws),self.digest,lambda _:True),'denied')
            self.assertIsNone(self.resolve())
    def test_parent_rebinding_or_change_denies(self):
        with hook_lineage.track_shared_child(self.scoped,self.parent.id,self.child.id,self.ws):
            self.cfg.write_text(json.dumps({'hooks':{'stop':'echo changed'}})); self.assertIsNone(self.resolve())
    def test_disabled_admin_invalidates_inherited_approval(self):
        with hook_lineage.track_shared_child(self.scoped,self.parent.id,self.child.id,self.ws):
            self.assertIsNotNone(self.resolve())
            with patch.object(main.auth_web.auth_store,'get',side_effect=lambda who:SimpleNamespace(enabled=(who!='admin'),role='admin' if who=='admin' else 'member')):
                self.assertIsNone(self.resolve())

    def test_parent_lookup_does_not_use_client_claim(self):
        self.assertIsNone(hook_lineage.parent_for_shared_child('owner',self.child.id,self.ws))
        with hook_lineage.track_shared_child(self.scoped,self.parent.id,self.child.id,self.ws):
            self.assertEqual(hook_lineage.parent_for_shared_child('owner',self.child.id,self.ws),self.parent.id)
            self.assertIsNone(hook_lineage.parent_for_shared_child('other',self.child.id,self.ws))
