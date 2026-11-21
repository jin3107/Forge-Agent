"""Conditional shared-child hook approval prototype; not an execution gate.

Every callback is trusted: callers must authenticate scope and establish a real
parent-child relationship server-side. Passing fabricated callbacks is not safe.
"""
from pathlib import Path
from . import hook_inspection
from .hook_approvals import Decision, Snapshot
from .session import _valid_session_id


def inherit_shared_snapshot(*, owner_id, parent_sid, child_sid, workspace,
                            resolve_parent, validate_shared_relation, inspect_child_scope):
    """Return the parent's approved snapshot only for a revalidated shared child.

    The callbacks must establish actual owner/session/workspace and trusted
    lineage, not values supplied by a client or a mutable session record. All
    checks can become stale immediately; effects cannot be rolled back.
    """
    try:
        if (not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 256 or
            not _valid_session_id(parent_sid) or not _valid_session_id(child_sid) or parent_sid == child_sid or
            not all(callable(x) for x in (resolve_parent, validate_shared_relation, inspect_child_scope))):
            return None
        supplied = Path(workspace)
        ws = supplied.resolve(strict=True)
        if not ws.is_dir() or not supplied.is_absolute() or supplied != ws or len(str(ws)) > 4096:
            return None

        def check():
            if validate_shared_relation(owner_id, parent_sid, child_sid, ws) is not True:
                return None
            parent = resolve_parent(owner_id, parent_sid, ws)
            if not isinstance(parent, tuple) or len(parent) != 2:
                return None
            decision, snapshot = parent
            if (not isinstance(decision, Decision) or not isinstance(snapshot, Snapshot) or
                decision.state != 'approved' or not isinstance(decision.authority_id, str) or
                not decision.authority_id or snapshot.state != 'hooks' or not snapshot.digest or
                decision.digest != snapshot.digest or not isinstance(snapshot.raw, bytes)):
                return None
            verified = hook_inspection.inspect_bytes(snapshot.raw)
            child = inspect_child_scope(owner_id, child_sid, ws)
            if (verified.state != 'hooks' or verified.digest != snapshot.digest or
                not isinstance(child, hook_inspection.Inspection) or child.state != 'hooks' or
                child.digest != snapshot.digest or child.raw != snapshot.raw):
                return None
            return parent

        first = check()
        if first is None or check() != first:
            return None
        return first
    except Exception:
        return None
