"""Workspace hook approval records are scoped and invalidated by changed bytes."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import hook_approvals


class HookApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = self.ws / '.forge' / 'config.json'
        self.cfg.parent.mkdir()
        self.raw = json.dumps({'hooks': {'user_prompt_submit': 'echo hello'}}).encode()
        self.cfg.write_bytes(self.raw)
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.store = hook_approvals.HookApprovals(max_entries=2)

    def approve(self, **kwargs):
        args = {'workspace': self.ws, 'owner_id':'alice', 'session_id':'s1', 'expected_digest':self.digest}
        args.update(kwargs)
        return self.store.approve(**args)

    def check(self, **kwargs):
        args = {'workspace':self.ws, 'owner_id':'alice', 'session_id':'s1'}
        args.update(kwargs)
        return self.store.check(**args)

    def test_approval_exact_scope_and_revocation(self):
        self.assertEqual(self.check().state, 'approval_required')
        self.assertEqual(self.approve().state, 'approved')
        self.assertEqual(self.check().state, 'approved')
        self.assertEqual(self.check(owner_id='bob').state, 'approval_required')
        self.assertEqual(self.check(session_id='s2').state, 'approval_required')
        other = self.ws / 'other'
        other.mkdir()
        (other / '.forge').mkdir()
        (other / '.forge' / 'config.json').write_bytes(self.raw)
        self.assertEqual(self.check(workspace=other).state, 'approval_required')
        self.store.revoke(workspace=self.ws, owner_id='alice', session_id='s1')
        self.assertEqual(self.check().state, 'approval_required')

    def test_stale_expected_and_new_config_require_reapproval(self):
        self.assertEqual(self.approve().state, 'approved')
        self.cfg.write_bytes(self.raw + b' ')
        self.assertEqual(self.check().state, 'approval_required')
        self.assertEqual(self.approve().state, 'changed')
        new_digest = hashlib.sha256(self.raw + b' ').hexdigest()
        self.assertEqual(self.approve(expected_digest=new_digest).state, 'approved')
        self.assertEqual(self.check().state, 'approved')

    def test_observed_change_invalidates_old_approval_even_if_bytes_restored(self):
        self.assertEqual(self.approve().state, 'approved')
        self.cfg.write_bytes(self.raw + b' ')
        self.assertEqual(self.check().state, 'approval_required')
        self.cfg.write_bytes(self.raw)
        self.assertEqual(self.check().state, 'approval_required')
        self.assertEqual(self.approve().state, 'approved')
        self.cfg.unlink()
        self.assertEqual(self.check().state, 'absent')
        self.cfg.write_bytes(self.raw)
        self.assertEqual(self.check().state, 'approval_required')

    def test_invalid_or_absent_cannot_be_approved_and_invalid_identity_denied(self):
        for bad in ('', 'A'*64, 'a'*63, 'g'*64, None):
            with self.subTest(digest=bad):
                self.assertEqual(self.approve(expected_digest=bad).state, 'invalid_request')
        self.assertEqual(self.check(owner_id='').state, 'invalid_request')
        self.assertEqual(self.approve(session_id='').state, 'invalid_request')
        self.cfg.write_text('{')
        self.assertEqual(self.check().state, 'invalid_config')
        self.assertEqual(self.approve().state, 'invalid_config')
        self.cfg.unlink()
        self.assertEqual(self.check().state, 'absent')
        self.assertEqual(self.approve().state, 'changed')
        self.cfg.write_text('{"hooks":{}}')
        self.assertEqual(self.check().state, 'no_hooks')
        self.assertEqual(self.approve().state, 'changed')

    def test_limit_and_overwrite_same_scope(self):
        self.assertEqual(self.approve().state, 'approved')
        self.assertEqual(self.approve(session_id='s2').state, 'approved')
        self.assertEqual(self.approve(session_id='s3').state, 'capacity')
        self.assertEqual(self.approve().state, 'approved')
        self.store.revoke(workspace=self.ws, owner_id='alice', session_id='s2')
        self.assertEqual(self.approve(session_id='s3').state, 'approved')

    def test_store_is_process_local_and_exceptions_fail_closed(self):
        self.assertEqual(self.approve().state, 'approved')
        self.assertEqual(hook_approvals.HookApprovals().check(self.ws, 'alice', 's1').state, 'approval_required')
        with patch('engine.hook_approvals.hook_inspection.inspect', side_effect=OSError('private error')):
            result = self.check()
        self.assertEqual(result.state, 'invalid_config')
        self.assertNotIn('private error', repr(result))

    def test_concurrent_admission_never_exceeds_cap(self):
        gate = threading.Barrier(9)
        results = []
        def worker(i):
            gate.wait()
            results.append(self.approve(session_id=f's{i}').state)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        gate.wait()
        for thread in threads: thread.join(timeout=3)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(results.count('approved'), 2)
        self.assertEqual(results.count('capacity'), 6)
