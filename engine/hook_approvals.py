"""Process-local, scoped workspace hook approval records (not an authorization API).

Not wired into hook execution. Callers must authenticate/authorize approval and
must not treat this registry as an OS sandbox or durable multi-worker store.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import hook_inspection


@dataclass(frozen=True)
class Decision:
    state: str
    digest: str | None = None
    # Prototype cache identity; not a credential or authentication boundary.
    authority_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Snapshot:
    state: str
    digest: str | None
    raw: bytes | None = field(repr=False)


def _scope(workspace: Path, owner_id: str, session_id: str) -> tuple[str, str, str] | None:
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 256:
        return None
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
        return None
    try:
        path = Path(workspace).resolve(strict=True)
        if not path.is_dir() or len(str(path)) > 4096:
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return owner_id, session_id, str(path)


class HookApprovals:
    """In-memory records: no implicit authentication and no cross-process sharing."""

    def __init__(self, max_entries: int = 1024):
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError('max_entries must be positive')
        self._limit = max_entries
        self._entries: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _inspect(path: str):
        try:
            return hook_inspection.inspect(Path(path))
        except Exception:
            # A broken inspector cannot authorize execution.
            return hook_inspection.Inspection('invalid')

    def check(self, workspace: Path, owner_id: str, session_id: str) -> Decision:
        return self.snapshot(workspace, owner_id, session_id)[0]

    def snapshot(self, workspace: Path, owner_id: str, session_id: str) -> tuple[Decision, Snapshot | None]:
        """Inspect once and release exact bytes only when eligible for use.

        This is a prototype, not authentication or a hook execution gate.
        """
        scope = _scope(workspace, owner_id, session_id)
        if scope is None:
            return Decision('invalid_request'), None
        with self._lock:
            result = self._inspect(scope[2])
            if result.state == 'invalid':
                self._entries.pop(scope, None)
                return Decision('invalid_config'), None
            if result.state == 'absent':
                self._entries.pop(scope, None)
                return Decision('absent'), Snapshot('absent', None, None)
            if result.state == 'no_hooks' and result.digest and result.raw is not None:
                self._entries.pop(scope, None)
                return Decision('no_hooks', result.digest), Snapshot('no_hooks', result.digest, result.raw)
            if result.state != 'hooks' or not result.digest or result.raw is None:
                self._entries.pop(scope, None)
                return Decision('invalid_config'), None
            if self._entries.get(scope) == result.digest:
                return Decision('approved', result.digest), Snapshot('hooks', result.digest, result.raw)
            self._entries.pop(scope, None)
            return Decision('approval_required', result.digest), None

    def approve(self, workspace: Path, owner_id: str, session_id: str, expected_digest: str) -> Decision:
        """Record only exact reviewed bytes; caller must authorize this action."""
        scope = _scope(workspace, owner_id, session_id)
        if scope is None or not isinstance(expected_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
            return Decision('invalid_request')
        with self._lock:
            result = self._inspect(scope[2])
            if result.state == 'invalid':
                self._entries.pop(scope, None)
                return Decision('invalid_config')
            if result.state != 'hooks' or result.digest != expected_digest:
                if result.state != 'hooks' or result.digest != self._entries.get(scope):
                    self._entries.pop(scope, None)
                return Decision('changed')
            if scope not in self._entries and len(self._entries) >= self._limit:
                return Decision('capacity')
            self._entries[scope] = expected_digest
            return Decision('approved', expected_digest)

    def revoke(self, workspace: Path, owner_id: str, session_id: str) -> Decision:
        scope = _scope(workspace, owner_id, session_id)
        if scope is None:
            return Decision('invalid_request')
        with self._lock:
            self._entries.pop(scope, None)
        return Decision('revoked')
