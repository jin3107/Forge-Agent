"""Inspection is side-effect-free and fails closed on ambiguous hook config."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import hook_inspection


class WorkspaceHookInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = self.ws / '.forge' / 'config.json'

    def write(self, data):
        self.cfg.parent.mkdir(exist_ok=True)
        self.cfg.write_bytes(data)

    def test_absent_no_hooks_and_exact_digest(self):
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'absent')
        raw = b'{"theme":"dark", "hooks": {}}\n'
        self.write(raw)
        result = hook_inspection.inspect(self.ws)
        self.assertEqual(result.state, 'no_hooks')
        self.assertEqual(result.digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.config, {'theme':'dark', 'hooks':{}})
        self.assertEqual(self.cfg.read_bytes(), raw)

    def test_digest_changes_with_bytes_and_exact_size_limit(self):
        first = b'{"hooks":{"stop":"echo hi"}}'
        self.write(first)
        a = hook_inspection.inspect(self.ws)
        self.write(first + b' ')
        b = hook_inspection.inspect(self.ws)
        self.assertEqual((a.state, b.state), ('hooks', 'hooks'))
        self.assertNotEqual(a.digest, b.digest)
        self.write(b'{}' + b' ' * (hook_inspection.MAX_CONFIG_BYTES - 2))
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'no_hooks')

    def test_empty_values_and_rejected_non_finite_numbers(self):
        for obj in ({}, {'hooks':None}, {'hooks':{}}, {'on_file_change':''}):
            with self.subTest(obj=obj):
                self.write(json.dumps(obj).encode())
                self.assertEqual(hook_inspection.inspect(self.ws).state, 'no_hooks')
        self.write(b'{"value":NaN}')
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'invalid')

    def test_empty_workspace_override_suppresses_global_hook_and_requires_review(self):
        from engine import hooks, hook_approvals
        global_cfg = {'hooks': {'pre_tool_use': [{'command': 'global-protection'}]}}
        store = hook_approvals.HookApprovals()
        for workspace_hooks in ({'stop': []}, {'pre_tool_use': []}, {'unknown': []}):
            with self.subTest(workspace_hooks=workspace_hooks):
                self.write(json.dumps({'hooks': workspace_hooks}).encode())
                with patch('engine.hooks.config.load_config', return_value=global_cfg):
                    self.assertEqual(hooks._hook_cfgs(self.ws, 'pre_tool_use'), [])
                result = hook_inspection.inspect(self.ws)
                self.assertEqual(result.state, 'hooks')
                decision = store.check(self.ws, 'alice', 's1')
                self.assertEqual(decision.state, 'approval_required')
                self.assertEqual(store.approve(self.ws, 'alice', 's1', decision.digest).state, 'approved')
                self.assertEqual(store.check(self.ws, 'alice', 's1').state, 'approved')
                store.revoke(self.ws, 'alice', 's1')

    def test_whitespace_legacy_value_also_overrides_global_selection(self):
        from engine import hooks
        self.write(json.dumps({'on_file_change': ' '}).encode())
        with patch('engine.hooks.config.load_config', return_value={'on_file_change':'global-legacy'}):
            selected = hooks._hook_cfgs(self.ws, 'post_tool_use')
        self.assertEqual(selected[0]['command'], ' ')
        self.assertTrue(selected[0]['_legacy_env'])
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'hooks')

    def test_empty_hooks_object_preserves_global_hook_fallback(self):
        from engine import hooks
        global_cfg = {'hooks': {'pre_tool_use': [{'command': 'global-protection'}]}}
        for config in ({}, {'hooks':None}, {'hooks':{}}):
            with self.subTest(config=config):
                self.write(json.dumps(config).encode())
                self.assertEqual(hook_inspection.inspect(self.ws).state, 'no_hooks')
                with patch('engine.hooks.config.load_config', return_value=global_cfg):
                    selected = hooks._hook_cfgs(self.ws, 'pre_tool_use')
                self.assertEqual(selected, [{'command': 'global-protection'}])

    def test_detects_hooks_unknown_event_and_legacy(self):
        for obj in ({'hooks':{'user_prompt_submit':[{'command':'echo hi'}]}},
                    {'hooks':{'custom_event':'echo hello'}},
                    {'on_file_change':'echo changed'}):
            with self.subTest(obj=obj):
                self.write(json.dumps(obj).encode())
                result = hook_inspection.inspect(self.ws)
                self.assertEqual(result.state, 'hooks')
                self.assertEqual(result.config, obj)

    def test_invalid_json_duplicate_keys_shapes_and_commands(self):
        invalid = [b'{', b'[]', b'\xff', b'{"hooks":{},"hooks":{}}',
                   b'{"hooks":[]}', b'{"hooks":{"pre_tool_use":42}}',
                   b'{"hooks":{"pre_tool_use":[{"command":42}]}}',
                   b'{"hooks":{"post_tool_use":[{"command":"x","matcher":{}}]}}',
                   b'{"on_file_change": ["echo x"]}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.write(raw)
                result = hook_inspection.inspect(self.ws)
                self.assertEqual(result.state, 'invalid')
                self.assertIsNone(result.digest)
                self.assertIsNone(result.config)

    def test_bounded_read_and_sanitized_io_error(self):
        self.write(b' ' * (hook_inspection.MAX_CONFIG_BYTES + 1))
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'invalid')
        with patch('engine.hook_inspection.os.open', side_effect=PermissionError('SECRET_PATH')):
            result = hook_inspection.inspect(self.ws)
        self.assertEqual(result.state, 'invalid')
        self.assertNotIn('SECRET_PATH', repr(result))

    @unittest.skipUnless(hasattr(os, 'symlink'), 'requires symlinks')
    def test_rejects_symlink_config_and_parent(self):
        target = self.ws / 'other.json'
        target.write_text('{"hooks":{}}')
        self.cfg.parent.mkdir()
        self.cfg.symlink_to(target)
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'invalid')
        self.cfg.unlink()
        self.cfg.parent.rmdir()
        other_dir = self.ws / 'otherdir'
        other_dir.mkdir()
        (other_dir / 'config.json').write_text('{}')
        self.cfg.parent.symlink_to(other_dir, target_is_directory=True)
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'invalid')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires FIFO')
    def test_rejects_fifo_without_blocking(self):
        self.cfg.parent.mkdir()
        os.mkfifo(self.cfg)
        self.assertEqual(hook_inspection.inspect(self.ws).state, 'invalid')

    def test_inspection_never_runs_command(self):
        marker = self.ws / 'marker'
        self.write(json.dumps({'hooks':{'session_start':f'touch {marker}'}}).encode())
        with patch('engine.hooks._run_cmd', side_effect=AssertionError('must not run')):
            result = hook_inspection.inspect(self.ws)
        self.assertEqual(result.state, 'hooks')
        self.assertFalse(marker.exists())
