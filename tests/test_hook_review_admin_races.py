"""Admin review must not disclose after an observed request revocation."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from engine.hook_review_requests import ReviewRequests
from engine import hook_inspection
from server import main

class AdminReviewRaces(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_while_inspecting_prevents_disclosure(self):
        with tempfile.TemporaryDirectory() as td:
            ws=Path(td); (ws/'.forge').mkdir()
            raw=b'{"hooks":{"stop":"sensitive"}}'
            (ws/'.forge'/'config.json').write_bytes(raw)
            registry=ReviewRequests()
            row=registry.submit('owner','sid',str(ws),hashlib.sha256(raw).hexdigest(),share_commands=True)
            real_inspect=hook_inspection.inspect
            def inspect_then_revoke(path):
                result=real_inspect(path)
                registry.revoke(row.id,'owner',False)
                return result
            with patch.object(main,'_hook_review_requests',registry), patch.object(main,'_current_requested_workspace',return_value=ws), patch.object(hook_inspection,'inspect',side_effect=inspect_then_revoke):
                with self.assertRaises(HTTPException) as ctx:
                    await main.admin_hook_review_request(row.id,user=None)
            self.assertEqual(ctx.exception.status_code,409)
            self.assertNotIn('sensitive',str(ctx.exception.detail))

    async def test_request_change_during_final_scope_check_prevents_disclosure(self):
        for action in ('revoke', 'replace', 'expire', 'decision'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                ws=Path(td); (ws/'.forge').mkdir()
                raw=b'{"hooks":{"stop":"sensitive"}}'
                (ws/'.forge'/'config.json').write_bytes(raw)
                clock=[100.0]
                registry=ReviewRequests(ttl=10, clock=lambda:clock[0])
                row=registry.submit('owner','sid',str(ws),hashlib.sha256(raw).hexdigest(),share_commands=True)
                calls=[0]
                def check_scope(_row):
                    calls[0]+=1
                    if calls[0] == 2:
                        if action == 'revoke':
                            registry.revoke(row.id,'owner',False)
                        elif action == 'replace':
                            registry.submit('owner','sid',str(ws),'b'*64,share_commands=True)
                        elif action == 'expire':
                            clock[0]+=11
                        else:
                            registry.decide(row.id,'admin','approved',str(ws),row.digest,lambda _:True)
                    return ws
                with patch.object(main,'_hook_review_requests',registry), patch.object(main,'_current_requested_workspace',side_effect=check_scope):
                    with self.assertRaises(HTTPException) as ctx:
                        await main.admin_hook_review_request(row.id,user=None)
                self.assertEqual(ctx.exception.status_code,409)
                self.assertNotIn('sensitive',str(ctx.exception.detail))

    async def test_observed_stale_request_is_invalidated(self):
        with tempfile.TemporaryDirectory() as td:
            ws=Path(td); (ws/'.forge').mkdir()
            original=b'{"hooks":{"stop":"original"}}'
            config=ws/'.forge'/'config.json'
            config.write_bytes(original)
            registry=ReviewRequests()
            row=registry.submit('owner','sid',str(ws),hashlib.sha256(original).hexdigest(),share_commands=True)
            self.assertEqual(registry.decide(row.id,'admin','approved',str(ws),row.digest,lambda _:True),'approved')
            config.write_bytes(b'{"hooks":{"stop":"changed"}}')
            with patch.object(main,'_hook_review_requests',registry), patch.object(main,'_current_requested_workspace',return_value=ws):
                with self.assertRaises(HTTPException) as ctx:
                    await main.admin_hook_review_request(row.id,user=None)
                self.assertEqual(ctx.exception.status_code,409)
                config.write_bytes(original)
                with self.assertRaises(HTTPException) as ctx:
                    await main.admin_hook_review_request(row.id,user=None)
                self.assertEqual(ctx.exception.status_code,404)

    async def test_stale_read_invalidates_same_request_even_if_decided_during_read(self):
        with tempfile.TemporaryDirectory() as td:
            ws=Path(td); (ws/'.forge').mkdir()
            raw=b'{"hooks":{"stop":"old"}}'
            (ws/'.forge'/'config.json').write_bytes(raw)
            registry=ReviewRequests()
            row=registry.submit('owner','sid',str(ws),hashlib.sha256(raw).hexdigest(),share_commands=True)
            def inspect_and_decide(path):
                self.assertEqual(registry.decide(row.id,'admin','approved',str(ws),row.digest,lambda _:True),'approved')
                return hook_inspection.Inspection('invalid')
            with patch.object(main,'_hook_review_requests',registry), patch.object(main,'_current_requested_workspace',return_value=ws), patch.object(hook_inspection,'inspect',side_effect=inspect_and_decide):
                with self.assertRaises(HTTPException):
                    await main.admin_hook_review_request(row.id,user=None)
            self.assertIsNone(registry.get(row.id))

    async def test_stale_read_does_not_remove_newer_request(self):
        with tempfile.TemporaryDirectory() as td:
            ws=Path(td); (ws/'.forge').mkdir()
            raw=b'{"hooks":{"stop":"old"}}'
            (ws/'.forge'/'config.json').write_bytes(raw)
            registry=ReviewRequests()
            old=registry.submit('owner','sid',str(ws),hashlib.sha256(raw).hexdigest(),share_commands=True)
            replacement=[]
            def inspect_and_replace(path):
                replacement.append(registry.submit('owner','sid',str(ws),'b'*64,share_commands=True))
                return hook_inspection.Inspection('invalid')
            with patch.object(main,'_hook_review_requests',registry), patch.object(main,'_current_requested_workspace',return_value=ws), patch.object(hook_inspection,'inspect',side_effect=inspect_and_replace):
                with self.assertRaises(HTTPException) as ctx:
                    await main.admin_hook_review_request(old.id,user=None)
                self.assertEqual(ctx.exception.status_code,409)
            self.assertEqual(registry.get(replacement[0].id),replacement[0])

    async def test_metadata_only_legacy_request_cannot_disclose(self):
        registry=ReviewRequests()
        row=registry.submit('owner','sid','/tmp','a'*64)
        with patch.object(main,'_hook_review_requests',registry):
            with self.assertRaises(HTTPException) as ctx:
                await main.admin_hook_review_request(row.id,user=None)
        self.assertEqual(ctx.exception.status_code,404)
