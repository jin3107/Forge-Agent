"""Process-local hook review requests and admin decisions.

Normal non-demo server chat consults decisions; other paths remain ungated.
Records are not durable or shared across workers.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable


@dataclass(frozen=True)
class ReviewRequest:
    id: str
    owner_id: str
    session_id: str
    workspace: str
    digest: str
    share_commands: bool
    expires_at: float  # monotonic clock, meaningful only in this process
    status: str = 'pending'
    decided_by: str | None = None


class ReviewRequests:
    def __init__(self, capacity: int = 128, ttl: float = 1800, clock: Callable[[], float] = time.monotonic):
        if capacity < 1 or ttl <= 0:
            raise ValueError('capacity and ttl must be positive')
        self._capacity, self._ttl, self._clock = capacity, ttl, clock
        self._lock = threading.RLock()
        self._entries: dict[str, ReviewRequest] = {}

    def _prune(self, now: float) -> None:
        for key in [key for key, row in self._entries.items() if row.expires_at <= now]:
            del self._entries[key]

    def submit(self, owner_id: str, session_id: str, workspace: str, digest: str, *, share_commands: bool = False) -> ReviewRequest | None:
        # Inputs must already have been authenticated and validated by the caller.
        with self._lock:
            now = self._clock()
            self._prune(now)
            existing = next((r for r in self._entries.values()
                             if r.owner_id == owner_id and r.session_id == session_id), None)
            if existing and (existing.workspace, existing.digest, existing.share_commands) == (workspace, digest, share_commands):
                return existing
            if existing:
                del self._entries[existing.id]
            if len(self._entries) >= self._capacity:
                return None
            row = ReviewRequest(uuid.uuid4().hex, owner_id, session_id, workspace, digest, share_commands, now + self._ttl)
            self._entries[row.id] = row
            return row

    def list(self) -> list[ReviewRequest]:
        with self._lock:
            self._prune(self._clock())
            return list(self._entries.values())

    def get(self, request_id: str) -> ReviewRequest | None:
        with self._lock:
            self._prune(self._clock())
            return self._entries.get(request_id)

    def decide(self, request_id: str, admin_id: str, decision: str, workspace: str, digest: str,
               validate: Callable[[ReviewRequest], bool]) -> str:
        """Record a decision while serializing with revocation/expiry.

        Caller must authorize the admin. Validation is synchronous and is run
        under the registry lock; this is not a cross-process transaction.
        """
        if decision not in ('approved', 'denied') or not admin_id:
            return 'invalid'
        with self._lock:
            self._prune(self._clock())
            row = self._entries.get(request_id)
            if not row or not row.share_commands:
                return 'missing'
            if (row.workspace, row.digest) != (workspace, digest):
                return 'changed'
            if row.status != 'pending' and (row.status != decision or row.decided_by != admin_id):
                return 'conflict'
            try:
                valid = validate(row)
            except Exception:
                valid = False
            self._prune(self._clock())
            if self._entries.get(request_id) != row:
                return 'missing'
            if not valid:
                del self._entries[request_id]
                return 'stale'
            self._entries[request_id] = replace(row, status=decision, decided_by=admin_id)
            return decision

    def approved_snapshot(self, owner_id: str, session_id: str, workspace: str,
                          validate: Callable[[ReviewRequest], bytes | None]):
        """Resolve an approved request to revalidated bytes, not an execution gate.

        Callers must authenticate the scope and perform all external account,
        session and workspace validation in `validate`. Registry operations are
        serialized in this process only; returned snapshots can become stale.
        """
        from . import hook_inspection
        from .hook_approvals import Decision, Snapshot
        with self._lock:
            self._prune(self._clock())
            row = next((r for r in self._entries.values()
                        if (r.owner_id, r.session_id, r.workspace) == (owner_id, session_id, workspace)), None)
            if not row or row.share_commands is not True or row.status != 'approved' or not row.decided_by:
                return None
            try:
                raw = validate(row)
                checked = hook_inspection.inspect_bytes(raw)
            except Exception:
                checked = hook_inspection.Inspection('invalid')
            self._prune(self._clock())
            if self._entries.get(row.id) != row:
                return None
            if checked.state != 'hooks' or checked.digest != row.digest or checked.raw is None:
                del self._entries[row.id]
                return None
            return Decision('approved', checked.digest, row.id), Snapshot('hooks', checked.digest, checked.raw)

    def scope_snapshot(self, owner_id: str, session_id: str, workspace: str,
                       inspect_scope, validate_admin):
        """Prototype scope resolution; caller supplies trusted checks and authentication.

        It does not enforce hook execution and is not a cross-process transaction.
        """
        from . import hook_inspection
        from .hook_approvals import Decision, Snapshot
        with self._lock:
            self._prune(self._clock())
            row = next((r for r in self._entries.values()
                        if (r.owner_id, r.session_id) == (owner_id, session_id)), None)
            try:
                observed = inspect_scope()
                if not isinstance(observed, hook_inspection.Inspection):
                    raise ValueError('invalid inspection')
                if observed.state == 'absent' and observed.digest is None and observed.raw is None and observed.config is None:
                    checked = observed
                else:
                    checked = hook_inspection.inspect_bytes(observed.raw)
                    if checked.state != observed.state or checked.digest != observed.digest:
                        raise ValueError('inconsistent inspection')
            except Exception:
                checked = hook_inspection.Inspection('invalid')
            self._prune(self._clock())
            current = next((r for r in self._entries.values()
                            if (r.owner_id, r.session_id) == (owner_id, session_id)), None)
            if current != row:
                return None
            if checked.state == 'invalid':
                if row:
                    del self._entries[row.id]
                return None
            if checked.state in ('absent', 'no_hooks'):
                if row:
                    del self._entries[row.id]
                return Decision(checked.state, checked.digest), Snapshot(checked.state, checked.digest, checked.raw)
            if checked.state != 'hooks' or checked.digest is None or checked.raw is None:
                if row:
                    del self._entries[row.id]
                return None
            if row and (row.workspace != workspace or row.digest != checked.digest):
                del self._entries[row.id]
                return None
            if not row or row.share_commands is not True or row.status != 'approved' or not row.decided_by:
                return None
            try:
                valid = validate_admin(row)
            except Exception:
                valid = False
            self._prune(self._clock())
            if self._entries.get(row.id) != row:
                return None
            if valid is not True:
                del self._entries[row.id]
                return None
            return Decision('approved', checked.digest, row.id), Snapshot('hooks', checked.digest, checked.raw)

    def invalidate(self, row: ReviewRequest) -> bool:
        """Invalidate an observed stale request without deleting its replacement.

        A decision can change during a read, so status is deliberately ignored;
        a replacement receives a different request ID.
        """
        with self._lock:
            self._prune(self._clock())
            current = self._entries.get(row.id)
            if current is None or (current.owner_id, current.session_id, current.workspace, current.digest,
                                   current.share_commands, current.expires_at) != (row.owner_id, row.session_id,
                                   row.workspace, row.digest, row.share_commands, row.expires_at):
                return False
            del self._entries[row.id]
            return True

    def revoke(self, request_id: str, owner_id: str, admin: bool) -> bool:
        with self._lock:
            self._prune(self._clock())
            row = self._entries.get(request_id)
            if not row or (not admin and row.owner_id != owner_id):
                return False
            del self._entries[request_id]
            return True
