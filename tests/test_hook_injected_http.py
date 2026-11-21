"""Reuse real HTTP scenarios with per-agent injection, no global hook patching."""
import importlib
import os
import unittest
from unittest.mock import patch

foreground=importlib.import_module('test_hook_inheritance_http')
background=importlib.import_module('test_hook_inheritance_background_http')

@unittest.skipUnless(os.name=='posix','POSIX shell quoting')
class InjectedRunnerHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_foreground(self):
        with patch.dict(os.environ,{'FORGE_TEST_INJECTED_HOOK_RUNNER':'1'}):
            await foreground.InheritanceHTTP.test_parent_approval_inherited_only_during_shared_child_and_revoked(self)
    async def test_background(self):
        with patch.dict(os.environ,{'FORGE_TEST_INJECTED_HOOK_RUNNER':'1'}):
            await background.BackgroundInheritanceHTTP.test_revocation_during_background_child_session_start(self)
