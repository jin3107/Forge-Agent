"""Explicit review requests are scoped, bounded, expiring and revocable."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from engine.hook_review_requests import ReviewRequests

class ReviewRequestsTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.queue = ReviewRequests(capacity=2, ttl=10, clock=lambda:self.now)

    def put(self, owner='u', sid='s', workspace='/workspace', digest='a'*64):
        return self.queue.submit(owner,sid,workspace,digest,share_commands=True)

    def test_duplicate_idempotent_changed_supersedes_and_scoped_revoke(self):
        a = self.put()
        self.assertEqual(self.put().id, a.id)
        self.assertFalse(self.queue.revoke(a.id, 'other', False))
        b = self.put(digest='b'*64)
        self.assertNotEqual(a.id, b.id)
        self.assertIsNone(self.queue.get(a.id))
        self.assertTrue(self.queue.revoke(b.id,'u',False))
        self.assertIsNone(self.queue.get(b.id))

    def test_capacity_expiry_and_admin_revoke(self):
        a = self.put()
        self.put('v','t')
        self.assertIsNone(self.put('w','x'))
        self.assertTrue(self.queue.revoke(a.id,'admin',True))
        self.assertIsNotNone(self.put('w','x'))
        self.now += 11
        self.assertEqual(self.queue.list(), [])

    def test_decision_rechecks_live_request_and_expiry(self):
        row = self.put()
        def revoke_during_validation(record):
            self.queue.revoke(record.id, record.owner_id, False)
            return True
        self.assertEqual(self.queue.decide(row.id, 'admin', 'approved', '/workspace', 'a'*64, revoke_during_validation), 'missing')
        row = self.put()
        def expire(record):
            self.now += 11
            return True
        self.assertEqual(self.queue.decide(row.id, 'admin', 'approved', '/workspace', 'a'*64, expire), 'missing')

    def test_only_matching_pending_request_gets_decision(self):
        row = self.put()
        self.assertEqual(self.queue.decide(row.id,'admin','approved','/wrong','a'*64,lambda _:True), 'changed')
        self.assertEqual(self.queue.decide(row.id,'admin','approved','/workspace','a'*64,lambda _:False), 'stale')
        self.assertIsNone(self.queue.get(row.id), 'observed stale approval must not revive')
        row = self.put()
        self.assertEqual(self.queue.decide(row.id,'admin','approved','/workspace','a'*64,lambda _:True), 'approved')
        self.assertEqual(self.queue.get(row.id).decided_by, 'admin')
        self.assertEqual(self.queue.decide(row.id,'admin','approved','/workspace','a'*64,lambda _:True), 'approved')
        self.assertEqual(self.queue.decide(row.id,'admin','denied','/workspace','a'*64,lambda _:True), 'conflict')
        self.queue.revoke(row.id,'u',False)
        self.assertIsNone(self.queue.get(row.id))

    def test_conflicting_concurrent_decisions_only_one_wins(self):
        row = self.put()
        def decide(decision):
            return self.queue.decide(row.id,'admin',decision,'/workspace','a'*64,lambda _:True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ('approved','denied')))
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(sum(x in ('approved','denied') for x in results),1)

    def test_concurrent_capacity(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda i:self.put(str(i), str(i)), range(20)))
        self.assertEqual(sum(r is not None for r in results),2)
        self.assertEqual(len(self.queue.list()),2)
