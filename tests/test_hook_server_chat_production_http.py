"""Real chat server runner wiring; fake provider only, no hook monkeypatch."""
import importlib
import os
import unittest
from unittest.mock import patch

foreground=importlib.import_module('test_hook_inheritance_http')
background=importlib.import_module('test_hook_inheritance_background_http')
write=importlib.import_module('test_hook_checked_write_http')

@unittest.skipUnless(os.name=='posix','POSIX shell quoting')
class ServerChatHookHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_foreground_inheritance_and_revocation(self):
        with patch.dict(os.environ,{'FORGE_TEST_PRODUCTION_RUNNER':'1'}):
            await foreground.InheritanceHTTP.test_parent_approval_inherited_only_during_shared_child_and_revoked(self)
    async def test_background_inheritance_and_revocation(self):
        with patch.dict(os.environ,{'FORGE_TEST_PRODUCTION_RUNNER':'1'}):
            await background.BackgroundInheritanceHTTP.test_revocation_during_background_child_session_start(self)
    async def test_file_change_blocks_legacy_hook_after_unapproved_config(self):
        with patch.dict(os.environ,{'FORGE_TEST_PRODUCTION_RUNNER':'1','FORGE_TEST_INJECTED_HOOK_RUNNER':'1'}):
            await write.CheckedWriteHTTP.test_config_change_during_tool_precedes_denial_and_global_hook(self)
