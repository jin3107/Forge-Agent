"""Checked hook adapters used by the checked non-demo server paths, not all paths.

These adapters trust their resolver and must not be used as an identity or
isolation boundary. A returned snapshot can become stale at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from . import hooks
from .hook_approvals import Decision, Snapshot
from .hook_snapshot import select_entries

_GENERAL_EVENTS = frozenset({'user_prompt_submit', 'stop', 'pre_compact', 'notification'})


def _prepare(workspace, owner_id, sid, event, resolve, global_config):
    if (not isinstance(owner_id, str) or not owner_id.strip() or
            not isinstance(sid, str) or not sid.strip() or not isinstance(event, str) or
            not (isinstance(global_config, dict) or callable(global_config)) or not callable(resolve)):
        raise hooks.HookTrustDenied()
    try:
        ws = Path(workspace).resolve(strict=True)
        if not ws.is_dir():
            raise ValueError('invalid workspace')
    except (OSError, RuntimeError, TypeError, ValueError):
        raise hooks.HookTrustDenied() from None

    def select():
        try:
            pair = resolve(owner_id, sid, ws)
            if (not isinstance(pair, tuple) or len(pair) != 2 or
                    not isinstance(pair[0], Decision) or not isinstance(pair[1], Snapshot)):
                raise ValueError('invalid snapshot')
            allowed = {('approved', 'hooks'), ('absent', 'absent'), ('no_hooks', 'no_hooks')}
            if (pair[0].state, pair[1].state) not in allowed:
                raise ValueError('unapproved snapshot')
            # Callable configs are loaded for every selection, including
            # rechecks. A supplied dictionary remains a static snapshot.
            current_config = global_config() if callable(global_config) else global_config
            entries = select_entries(pair[0], pair[1], event, current_config)
            return pair, entries
        except Exception:
            raise hooks.HookTrustDenied() from None

    pair, entries = select()
    def recheck():
        current, current_entries = select()
        if current != pair or current_entries != entries:
            raise hooks.HookTrustDenied()
    return ws, pair, entries, recheck


async def run_checked_event(workspace: Path, owner_id: str, sid: str, event: str,
                            extra: dict | None,
                            resolve: Callable[[str, str, Path], tuple[Decision, Snapshot] | None],
                            global_config: dict) -> list[dict]:
    """Run general events from resolver-checked bytes; not production enforcement."""
    if not isinstance(event, str) or event not in _GENERAL_EVENTS or (extra is not None and not isinstance(extra, dict)):
        raise hooks.HookTrustDenied()
    ws, _pair, entries, recheck = _prepare(workspace, owner_id, sid, event, resolve, global_config)
    payload = {**(extra or {}), 'session_id': sid, 'hook_event_name': event, 'workspace': str(ws)}
    results = []
    for index, entry in enumerate(entries):
        if index:
            recheck()
        command = entry['command']
        result = await hooks._run_cmd(command, payload, ws)
        # Detect persistent revocation or configuration changes during even
        # the last command; this cannot undo effects or close TOCTOU races.
        recheck()
        results.append({'command': command, 'rc': result.get('code', 1),
                        'out': result.get('out', ''), 'err': result.get('err', '')})
    if not entries:
        recheck()
    return results


async def checked_pre_tool_use(workspace: Path, owner_id: str, sid: str, tool_name: str,
                               tool_input: dict, resolve, global_config: dict) -> dict | None:
    """Prototype pre-tool hook execution; caller must enforce its result."""
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise hooks.HookTrustDenied()
    ws, _pair, entries, recheck = _prepare(workspace, owner_id, sid, 'pre_tool_use', resolve, global_config)
    ran = False
    for entry in entries:
        if not hooks._matched(entry, tool_name):
            continue
        if ran:
            recheck()
        payload = {'session_id': sid, 'hook_event_name': 'PreToolUse',
                   'tool_name': tool_name, 'tool_input': tool_input, 'workspace': str(ws)}
        result = await hooks._run_cmd(entry['command'], payload, ws)
        ran = True
        recheck()
        if result['code'] == 2:
            return {'blocked': True, 'reason': result['err'] or result['out'] or
                    'bị chặn bởi hook pre_tool_use (exit 2)'}
    if not ran:
        recheck()
    return None


async def checked_post_tool_use(workspace: Path, owner_id: str, sid: str, tool_name: str,
                                tool_input: dict, tool_output: str, resolve, global_config: dict,
                                env_extra: dict | None = None) -> str:
    """Prototype post-tool hook execution; tool effects cannot be rolled back."""
    if (not isinstance(tool_name, str) or not isinstance(tool_input, dict) or
            not isinstance(tool_output, str) or (env_extra is not None and not isinstance(env_extra, dict))):
        raise hooks.HookTrustDenied()
    ws, _pair, entries, recheck = _prepare(workspace, owner_id, sid, 'post_tool_use', resolve, global_config)
    extras = []
    ran = False
    for entry in entries:
        if not hooks._matched(entry, tool_name) or entry.get('_legacy_env'):
            continue
        if ran:
            recheck()
        payload = {'session_id': sid, 'hook_event_name': 'PostToolUse',
                   'tool_name': tool_name, 'tool_input': tool_input,
                   'tool_output': tool_output[:4000], 'workspace': str(ws)}
        result = await hooks._run_cmd(entry['command'], payload, ws, env_extra)
        ran = True
        recheck()
        message = result['err'] or result['out']
        if message:
            extras.append(f"🔗 Hook PostToolUse (exit {result['code']}): {message}")
    if not ran:
        recheck()
    return '\n'.join(extras)


class CheckedSessionStartCache:
    """Bounded single-event-loop prototype; not wired to product execution.

    The resolver must provide an approval identity for hook-bearing snapshots.
    That identity is only a cache discriminator, never an authorization credential.
    Concurrent misses may each run; side effects cannot be rolled back.
    """
    def __init__(self, capacity: int = 128):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self._capacity = capacity
        self._cache = OrderedDict()
        self._pending = {}

    def __len__(self):
        return len(self._cache)

    async def run(self, workspace: Path, owner_id: str, sid: str, resolve, global_config: dict) -> str:
        if not isinstance(owner_id, str) or not owner_id.strip() or not isinstance(sid, str) or not sid.strip():
            raise hooks.HookTrustDenied()
        scope = (owner_id, sid)
        token = object()
        self._pending[scope] = token
        try:
            try:
                ws, pair, entries, recheck = _prepare(workspace, owner_id, sid, 'session_start', resolve, global_config)
                decision, snapshot = pair
                if decision.state == 'approved' and (not isinstance(decision.authority_id, str) or not decision.authority_id):
                    raise hooks.HookTrustDenied()
                encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode('utf-8')
                fingerprint = hashlib.sha256(encoded).hexdigest()
                identity = (str(ws), decision.state, decision.digest, decision.authority_id,
                            snapshot.state, snapshot.digest, fingerprint)
            except Exception:
                self._cache.pop(scope, None)
                raise hooks.HookTrustDenied() from None
            cached = self._cache.get(scope)
            if cached and cached[0] == identity:
                self._cache.move_to_end(scope)
                return cached[1]
            parts = []
            for index, entry in enumerate(entries):
                if index:
                    recheck()
                result = await hooks._run_cmd(entry['command'], {
                    'session_id': sid, 'hook_event_name': 'SessionStart', 'workspace': str(ws),
                }, ws)
                if result['out']:
                    parts.append(result['out'])
            # A completed command is not rolled back if this recheck fails.
            recheck()
            output = '\n'.join(parts)[:2000]
            if self._pending.get(scope) is token:
                self._cache[scope] = (identity, output)
                self._cache.move_to_end(scope)
                while len(self._cache) > self._capacity:
                    self._cache.popitem(last=False)
            return output
        except hooks.HookTrustDenied:
            if self._pending.get(scope) is token:
                self._cache.pop(scope, None)
            raise
        finally:
            if self._pending.get(scope) is token:
                self._pending.pop(scope, None)


_LEGACY_TIMEOUT = 30.0
_LEGACY_REAP_TIMEOUT = 1.0
_LEGACY_CAPTURE_BYTES = 64 * 1024
_LEGACY_READ_BYTES = 8192


async def _capture_legacy_output(proc):
    """Drain the merged stream while retaining only a bounded byte prefix."""
    captured = bytearray()
    truncated = False
    while True:
        chunk = await proc.stdout.read(_LEGACY_READ_BYTES)
        if not chunk:
            break
        room = max(0, _LEGACY_CAPTURE_BYTES - len(captured))
        captured.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    await proc.wait()
    return bytes(captured), truncated


async def _stop_legacy_process(proc):
    """Best effort only: escaped descendants and interrupted cleanup can survive."""
    if os.name == 'posix':
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
    async def drain():
        while await proc.stdout.read(_LEGACY_READ_BYTES):
            pass
    async def drain_and_wait():
        tasks = [asyncio.create_task(drain()), asyncio.create_task(proc.wait())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(drain_and_wait(), timeout=_LEGACY_REAP_TIMEOUT)
    except (asyncio.TimeoutError, OSError):
        pass


async def checked_global_file_change(workspace: Path, owner_id: str, sid: str,
                                     changed_path: str, resolve, global_config) -> str:
    """Experimental global legacy hook; available through opt-in injection.

    The caller must supply the actual server-wide config and trusted resolver.
    Called after a file write, so denial cannot reverse that write or a hook.
    """
    if not isinstance(changed_path, str) or not changed_path:
        raise hooks.HookTrustDenied()
    ws, _pair, _entries, recheck = _prepare(workspace, owner_id, sid, 'post_tool_use', resolve, global_config)
    def read_command():
        try:
            cfg = global_config() if callable(global_config) else global_config
            if not isinstance(cfg, dict):
                raise ValueError('invalid global config')
            cmd = cfg.get('on_file_change')
            if cmd is not None and not isinstance(cmd, str):
                raise ValueError('invalid global hook')
            return cmd
        except Exception:
            raise hooks.HookTrustDenied() from None
    command = read_command()
    def verify():
        recheck()
        if read_command() != command:
            raise hooks.HookTrustDenied()
    verify()
    if not command or not command.strip():
        return ''
    from .secrets import sanitized_env
    env = sanitized_env({'FORGE_CHANGED_FILE': changed_path, 'FORGE_WORKSPACE': str(ws)})
    try:
        proc = await asyncio.create_subprocess_shell(command, cwd=str(ws), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    **({'start_new_session': True} if os.name == 'posix' else {}))
        try:
            out, truncated = await asyncio.wait_for(_capture_legacy_output(proc), timeout=_LEGACY_TIMEOUT)
            decoded = out.decode('utf-8', 'replace').strip()
            text = decoded[:600] or '(không output)'
            if truncated or len(decoded) > 600:
                text += ' [output truncated]'
            rc = proc.returncode or 0
        except asyncio.TimeoutError:
            await _stop_legacy_process(proc)
            text, rc = f'timeout {_LEGACY_TIMEOUT:g}s; process cleanup unconfirmed', 124
        except asyncio.CancelledError:
            await _stop_legacy_process(proc)
            raise
        except Exception:
            await _stop_legacy_process(proc)
            raise
        message = f'\n🔗 Hook on_file_change (exit {rc}): ' + (text or '(không output)')
    except Exception:
        message = '\n🔗 Hook on_file_change lỗi: execution or cleanup unconfirmed'
    verify()
    return message
