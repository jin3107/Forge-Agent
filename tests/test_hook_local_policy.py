"""Local-only selector policy tests; not a server execution gate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks, hook_inspection


class LocalSelectorPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.config=self.ws/'.forge'/'config.json'
        self.local=patch.object(hooks,'_LOCAL_HOOK_POLICY',True,create=True); self.local.start(); self.addCleanup(self.local.stop)

    def select(self,event,global_config=None):
        with patch.object(hooks.config,'load_config',return_value=global_config or {}):
            return hooks._hook_cfgs(self.ws,event)

    def test_workspace_hooks_never_selected_even_for_unrelated_event(self):
        for cfg in ({'hooks':{'stop':'danger'}},{'on_file_change':'danger'},{'hooks':{'unknown':[]}}):
            self.config.write_text(json.dumps(cfg))
            for event in ('user_prompt_submit','session_start','pre_tool_use','post_tool_use','stop'):
                with self.subTest(cfg=cfg,event=event),self.assertRaises(hooks.HookTrustDenied):
                    self.select(event,{'hooks':{event:'global'}})

    def test_absent_and_hook_free_config_keep_global_selection(self):
        for raw in (None,'{}','{"hooks":{}}'):
            if raw is None: self.config.unlink(missing_ok=True)
            else: self.config.write_text(raw)
            self.assertEqual(self.select('stop',{'hooks':{'stop':'global'}}),[{'command':'global'}])

    def test_malformed_and_symlink_config_fails_closed(self):
        self.config.write_text('{')
        with self.assertRaises(hooks.HookTrustDenied): self.select('stop')
        self.config.unlink(); target=self.ws/'target'; target.write_text('{}')
        try: self.config.symlink_to(target)
        except (OSError,NotImplementedError): self.skipTest('symlinks unsupported')
        with self.assertRaises(hooks.HookTrustDenied): self.select('stop')

    def test_config_replaced_after_inspection_does_not_select_new_workspace_command(self):
        self.config.write_text('{}'); inspect=hook_inspection.inspect
        def replaced(ws):
            result=inspect(ws)
            self.config.write_text('{"hooks":{"stop":"unapproved"}}')
            return result
        with patch.object(hook_inspection,'inspect',side_effect=replaced):
            self.assertEqual(self.select('stop',{'hooks':{'stop':'global'}}),[{'command':'global'}])
        with self.assertRaises(hooks.HookTrustDenied): self.select('stop')

    def test_tui_launcher_enables_policy_before_running_app(self):
        try:
            from tui import app
        except ImportError:
            self.skipTest('optional textual dependency not installed')
        def run_app(_self):
            self.assertTrue(hooks._LOCAL_HOOK_POLICY)
            with self.assertRaises(hooks.HookTrustDenied):
                self.select('stop')
        self.config.write_text('{"hooks":{"stop":"danger"}}')
        with patch.object(hooks,'_LOCAL_HOOK_POLICY',False), patch.object(app.ForgeTUI,'run',run_app):
            app.run()

    def test_server_default_legacy_selector_is_not_changed_by_local_policy(self):
        self.config.write_text('{"hooks":{"stop":"workspace"}}')
        with patch.object(hooks,'_LOCAL_HOOK_POLICY',False,create=True):
            self.assertEqual(self.select('stop'),[{'command':'workspace'}])


class LocalEntrypoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws=Path(self.tmp.name).resolve(); (self.ws/'.forge').mkdir()
        self.config=self.ws/'.forge'/'config.json'
        self.config.write_text('{"hooks":{"stop":"danger"}}')
        self.policy=patch.object(hooks,'_LOCAL_HOOK_POLICY',True); self.policy.start(); self.addCleanup(self.policy.stop)
        hooks._SS_CACHE.clear(); self.addCleanup(hooks._SS_CACHE.clear)

    async def test_all_lifecycle_callers_deny_without_running_hook(self):
        from unittest.mock import AsyncMock
        with patch.object(hooks,'_run_cmd',new_callable=AsyncMock) as run:
            calls=[lambda:hooks.run_event(self.ws,'sid','user_prompt_submit'),
                   lambda:hooks.pre_tool_use(self.ws,'sid','write_file',{}),
                   lambda:hooks.post_tool_use(self.ws,'sid','write_file',{},'out'),
                   lambda:hooks.session_start_cache(self.ws,'sid'),
                   lambda:hooks.session_start(self.ws,'sid')]
            for call in calls:
                with self.assertRaises(hooks.LocalHookTrustDenied): await call()
            run.assert_not_awaited()

    async def test_existing_session_start_cache_cannot_bypass_local_check(self):
        hooks._SS_CACHE['sid']=(str(self.ws),'stale','previous')
        with self.assertRaises(hooks.LocalHookTrustDenied):
            await hooks.session_start_cache(self.ws,'sid')
