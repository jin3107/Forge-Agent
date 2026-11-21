"""Prototype selection uses the same inspected bytes that were approved."""
import hashlib
import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hook_approvals, hook_inspection, hooks
from engine.hook_snapshot import select_entries


class HookSnapshotSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.config = self.ws/'.forge'/'config.json'
        self.config.parent.mkdir()
        self.store = hook_approvals.HookApprovals()
        self.global_config = {'hooks': {'pre_tool_use': [{'command':'global'}], 'stop': 'global-stop'},
                              'on_file_change':'global-legacy'}

    def write(self, obj):
        raw = json.dumps(obj).encode()
        self.config.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_unapproved_and_invalid_never_release_snapshot(self):
        self.write({'hooks':{'pre_tool_use':'untrusted'}})
        decision, snapshot = self.store.snapshot(self.ws, 'alice', 's')
        self.assertEqual(decision.state, 'approval_required')
        self.assertIsNone(snapshot)
        self.config.write_text('{')
        decision, snapshot = self.store.snapshot(self.ws, 'alice', 's')
        self.assertEqual(decision.state, 'invalid_config')
        self.assertIsNone(snapshot)

    def test_approved_snapshot_does_not_reread_replaced_file(self):
        digest = self.write({'hooks':{'pre_tool_use':[{'command':'reviewed'}]}})
        self.assertEqual(self.store.approve(self.ws,'alice','s',digest).state, 'approved')
        decision, snapshot = self.store.snapshot(self.ws,'alice','s')
        self.write({'hooks':{'pre_tool_use':[{'command':'changed'}]}})
        selected = select_entries(decision, snapshot, 'pre_tool_use', self.global_config)
        self.assertEqual([x['command'] for x in selected], ['reviewed'])
        next_decision, next_snapshot = self.store.snapshot(self.ws,'alice','s')
        self.assertEqual(next_decision.state, 'approval_required')
        self.assertIsNone(next_snapshot)

    def test_valid_config_matches_legacy_selector_including_overrides(self):
        for cfg in ({}, {'hooks':{}}, {'hooks':{'stop':[]}},
                    {'hooks':{'pre_tool_use':['one', {'command':'two','matcher':'write'}]}},
                    {'on_file_change':'legacy-workspace'}):
            with self.subTest(config=cfg):
                digest = self.write(cfg)
                inspected = hook_inspection.inspect(self.ws)
                if inspected.state == 'hooks':
                    self.assertEqual(self.store.approve(self.ws,'alice','s',digest).state, 'approved')
                decision, snapshot = self.store.snapshot(self.ws,'alice','s')
                for event in ('pre_tool_use','post_tool_use','stop','unknown'):
                    with patch('engine.hooks.config.load_config', return_value=self.global_config):
                        old = hooks._hook_cfgs(self.ws, event)
                    new = select_entries(decision, snapshot, event, self.global_config)
                    self.assertEqual(new, old)
                self.store.revoke(self.ws,'alice','s')

    def test_no_config_uses_global_and_rejects_invalid_global(self):
        decision, snapshot = self.store.snapshot(self.ws, 'alice', 's')
        self.assertEqual(decision.state,'absent')
        self.assertEqual(select_entries(decision,snapshot,'pre_tool_use',self.global_config), [{'command':'global'}])
        with self.assertRaises(ValueError):
            select_entries(decision,snapshot,'pre_tool_use',{'hooks':{'pre_tool_use':{'unexpected':'value'}}})

    def test_decision_and_snapshot_must_match(self):
        digest = self.write({'hooks':{'stop':'hi'}})
        self.store.approve(self.ws,'alice','s',digest)
        decision, snapshot = self.store.snapshot(self.ws,'alice','s')
        with self.assertRaises(ValueError):
            select_entries(hook_approvals.Decision('approval_required',digest),snapshot,'stop',{})
        with self.assertRaises(ValueError):
            select_entries(hook_approvals.Decision('approved','0'*64),snapshot,'stop',{})
        with self.assertRaises(ValueError):
            select_entries(decision,None,'stop',{})
        with self.assertRaises(ValueError):
            select_entries(decision,replace(snapshot,raw=b'{"hooks":{"stop":"tampered"}}'),'stop',{})

    def test_snapshot_state_and_full_grammar_must_match_bytes(self):
        cases = [
            (b'{"hooks":{"stop":"danger"}}', 'no_hooks'),
            (b'{"on_file_change":"danger"}', 'no_hooks'),
            (b'{}', 'hooks'),
            (b'{"hooks":{"stop":"one"},"hooks":{}}', 'no_hooks'),
            (b'{"other":NaN}', 'no_hooks'),
            (b'{"hooks":{"stop":"ok","pre_tool_use":42}}', 'hooks'),
            (b' ' * (hook_inspection.MAX_CONFIG_BYTES + 1) + b'{}', 'no_hooks'),
        ]
        for raw, state in cases:
            with self.subTest(raw=raw[:100], state=state):
                digest = hashlib.sha256(raw).hexdigest()
                decision = hook_approvals.Decision('approved' if state == 'hooks' else 'no_hooks', digest)
                snapshot = hook_approvals.Snapshot(state, digest, raw)
                with self.assertRaises(ValueError):
                    select_entries(decision, snapshot, 'stop', self.global_config)

    def test_result_is_detached_and_selection_does_not_execute(self):
        digest = self.write({'hooks':{'pre_tool_use':[{'command':'safe','matcher':'write'}]}})
        self.store.approve(self.ws,'alice','s',digest)
        decision, snapshot = self.store.snapshot(self.ws,'alice','s')
        with patch('engine.hooks._run_cmd', side_effect=AssertionError('should not execute')):
            first = select_entries(decision,snapshot,'pre_tool_use',self.global_config)
            first[0]['command'] = 'mutated'
            second = select_entries(decision,snapshot,'pre_tool_use',self.global_config)
        self.assertEqual(second[0]['command'], 'safe')
        self.assertNotIn('safe', repr(snapshot), 'snapshot repr should not leak config contents')
