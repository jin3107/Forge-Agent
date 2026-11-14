"""Conditional shared-child inheritance prototype; no production gate."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from engine import hook_inheritance
from engine.hook_approvals import Decision, Snapshot
from engine.hook_inspection import inspect

class HookInheritance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.cfg=self.ws/'.forge'/'config.json'; self.raw=json.dumps({'hooks':{'stop':'echo ok'}}).encode(); self.cfg.write_bytes(self.raw)
        self.digest=hashlib.sha256(self.raw).hexdigest()
        self.pair=(Decision('approved',self.digest,'approval-id'),Snapshot('hooks',self.digest,self.raw))
        self.parent=self.pair; self.relation=True; self.child_path=self.ws
        self.calls=[]
    def resolve(self,owner,parent,ws):
        self.calls.append(('parent',owner,parent,ws)); return self.parent
    def relation_ok(self,owner,parent,child,ws):
        self.calls.append(('relation',owner,parent,child,ws)); return self.relation
    def child_scope(self,owner,child,ws):
        self.calls.append(('child',owner,child,ws))
        return inspect(self.child_path) if self.child_path == ws else None
    def run_resolver(self,**overrides):
        args=dict(owner_id='owner',parent_sid='parent',child_sid='child',workspace=self.ws,
                  resolve_parent=self.resolve, validate_shared_relation=self.relation_ok, inspect_child_scope=self.child_scope)
        args.update(overrides); return hook_inheritance.inherit_shared_snapshot(**args)
    def test_valid_and_revalidation(self):
        self.assertEqual(self.run_resolver(),self.pair)
        self.assertEqual(sum(c[0]=='parent' for c in self.calls),2)
        self.assertEqual(sum(c[0]=='child' for c in self.calls),2)
        self.assertEqual(sum(c[0]=='relation' for c in self.calls),2)
    def test_changed_config_revocation_distinct_workspace_and_no_relation(self):
        self.cfg.write_bytes(b'{"hooks":{"stop":"different"}}'); self.assertIsNone(self.run_resolver())
        self.cfg.write_bytes(self.raw); self.parent=None; self.assertIsNone(self.run_resolver())
        self.parent=self.pair; self.relation=False; self.assertIsNone(self.run_resolver())
        self.relation=True; self.child_path=self.ws/'other'; self.assertIsNone(self.run_resolver())
    def test_unapproved_or_forged_parent_and_self_scope_rejected(self):
        for pair in [(Decision('approved',self.digest),self.pair[1]),
                     (Decision('no_hooks',self.digest),self.pair[1]),
                     (Decision('approved',self.digest,'approval-id'),Snapshot('hooks',self.digest,b'{}'))]:
            self.parent=pair; self.assertIsNone(self.run_resolver())
        self.parent=self.pair
        for options in ({'parent_sid':'child'},{'owner_id':''},{'child_sid':'../bad'}, {'workspace':self.ws/'missing'}):
            self.assertIsNone(self.run_resolver(**options))
    def test_failure_and_mid_check_revoke_do_not_authorize(self):
        def bad(*_): raise OSError('private')
        self.assertIsNone(self.run_resolver(resolve_parent=bad))
        n=0
        def revoke(*args):
            nonlocal n
            n+=1
            return self.pair if n==1 else None
        self.assertIsNone(self.run_resolver(resolve_parent=revoke))
        n=0
        def changed(*args):
            nonlocal n
            n+=1
            if n==2: self.cfg.write_bytes(b'{}')
            return inspect(self.ws)
        self.assertIsNone(self.run_resolver(inspect_child_scope=changed))
