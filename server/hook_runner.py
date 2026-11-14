"""Scoped checked-hook adapter used by normal non-demo server chat.

Other entry points remain ungated. The caller must authenticate the owner and
supply a trustworthy resolver. This adapter is not an OS sandbox.
"""
from engine import hook_checked_runner, hooks


class ScopedCheckedHookRunner:
    def __init__(self, *, owner_id, resolve, load_config):
        if (not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 256 or
                not callable(resolve) or not callable(load_config)):
            raise ValueError('Invalid hook runner scope or dependencies')
        self._owner_id = owner_id
        self._resolve = resolve
        self._load_config = load_config
        self._cache = hook_checked_runner.CheckedSessionStartCache()

    def _config(self):
        try:
            value = self._load_config()
            if not isinstance(value, dict):
                raise ValueError('invalid config')
            return value
        except Exception:
            raise hooks.HookTrustDenied() from None

    async def run_event(self, workspace, sid, event, extra=None):
        return await hook_checked_runner.run_checked_event(
            workspace, self._owner_id, sid, event, extra, self._resolve, self._config)

    async def session_start_cache(self, workspace, sid):
        return await self._cache.run(workspace, self._owner_id, sid, self._resolve, self._config)

    async def pre_tool_use(self, workspace, sid, tool_name, tool_input):
        return await hook_checked_runner.checked_pre_tool_use(
            workspace, self._owner_id, sid, tool_name, tool_input, self._resolve, self._config)

    async def post_tool_use(self, workspace, sid, tool_name, tool_input, tool_output, env_extra=None):
        return await hook_checked_runner.checked_post_tool_use(
            workspace, self._owner_id, sid, tool_name, tool_input, tool_output,
            self._resolve, self._config, env_extra)

    async def global_file_change(self, workspace, sid, changed_path):
        return await hook_checked_runner.checked_global_file_change(
            workspace, self._owner_id, sid, changed_path, self._resolve, self._config)
