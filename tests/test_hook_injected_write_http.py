"""Checked legacy callback at the real tool boundary, via opt-in HTTP fixture."""
import importlib
import os
import unittest
from unittest.mock import patch

checked_write=importlib.import_module('test_hook_checked_write_http')

@unittest.skipUnless(os.name=='posix','POSIX shell quoting')
class InjectedWriteHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_config_write_denies_before_global_legacy_hook(self):
        with patch.dict(os.environ,{'FORGE_TEST_INJECTED_HOOK_RUNNER':'1'}):
            await checked_write.CheckedWriteHTTP.test_config_change_during_tool_precedes_denial_and_global_hook(self)
