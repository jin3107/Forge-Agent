"""Unresolvable workspace identities are not presented as valid reviews."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from server.main import workspace_hook_review

class HookReviewIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_non_directory_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file = root/'not-a-directory'
            file.write_text('data')
            for path in (root/'missing-secret-path', file):
                with self.subTest(path=path), patch('server.main._ws', return_value=path):
                    with self.assertRaises(HTTPException) as ctx:
                        await workspace_hook_review('session', user=None)
                    self.assertEqual(ctx.exception.status_code, 409)
                    self.assertNotIn('secret-path', str(ctx.exception.detail))
