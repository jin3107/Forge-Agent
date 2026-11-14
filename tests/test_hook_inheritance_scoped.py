"""Prototype inheritance with real scoped sessions and process-local reviews.

Lineage here is test-supplied, not a production trust source.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import config, hook_inheritance, hook_inspection
from engine.hook_review_requests import ReviewRequests
from engine.session import SessionStore, ScopedSessionStore
from server import main

class ScopedInheritance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); (root/'sessions').mkdir(); (root/'workspaces').mkdir()
        p=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.base=SessionStore(); self.store=ScopedSessionStore(self.base,'owner'); self.other=ScopedSessionStore(self.base,'other')
        self.parent=self.store.create(model='test'); self.child=self.store.create(model='test'); self.foreign=self.other.create(model='test')
        self.ws=self.store.workspace(self.parent.id).resolve(); self.store.open_workspace(self.child.id,str(self.ws))
        (self.ws/'.forge').mkdir(); (self.ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'stop':'echo ok'}}))
        digest=hook_inspection.inspect(self.ws).digest; self.reviews=ReviewRequests()
        row=self.reviews.submit('owner',self.parent.id,str(self.ws),digest,share_commands=True)
        self.row=row; self.assertEqual(self.reviews.decide(row.id,'admin','approved',str(self.ws),digest,lambda _:True),'approved')
        self.patches=[patch.object(main,'store',self.base),patch.object(main,'_hook_review_requests',self.reviews),
                      patch.object(main.auth_web.auth_store,'get',side_effect=lambda who:SimpleNamespace(enabled=True,role='admin' if who=='admin' else 'user'))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.lineage={('owner',self.parent.id,self.child.id,str(self.ws))}
    def relation(self,owner,parent,child,ws): return (owner,parent,child,str(ws)) in self.lineage
    def inspect_child(self,owner,child,ws):
        actual=main._current_requested_workspace(SimpleNamespace(owner_id=owner,session_id=child,workspace=str(ws)))
        return hook_inspection.inspect(actual) if actual is not None else None
    def run_resolver(self,child=None):
        return hook_inheritance.inherit_shared_snapshot(owner_id='owner',parent_sid=self.parent.id,child_sid=child or self.child.id,workspace=self.ws,
               resolve_parent=lambda owner,parent,ws:main._hook_scope_snapshot(owner,parent,str(ws)),validate_shared_relation=self.relation,inspect_child_scope=self.inspect_child)
    def test_real_scopes_and_revocation(self):
        self.assertIsNotNone(self.run_resolver())
        self.lineage.add(('owner',self.parent.id,self.foreign.id,str(self.ws)))
        self.assertIsNone(self.run_resolver(self.foreign.id))
        self.store.close_workspace(self.child.id); self.assertIsNone(self.run_resolver())
        self.store.open_workspace(self.child.id,str(self.ws)); self.assertIsNotNone(self.run_resolver())
        self.assertTrue(self.reviews.revoke(self.row.id,'owner',False)); self.assertIsNone(self.run_resolver())
