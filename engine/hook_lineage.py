"""Process-local active shared-child records, not an authorization boundary.

Only trusted internal code may issue records. No endpoint accepts lineage from a
client. The records are not persisted or shared between workers and must not be
used as proof against untrusted Python code or filesystem writers.
"""
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .session import ScopedSessionStore, _valid_session_id

_LIMIT = 1024
_lock = RLock()
_active = {}


def _key(owner, parent, child, workspace):
    if (not isinstance(owner, str) or not owner.strip() or len(owner) > 256 or
        not _valid_session_id(parent) or not _valid_session_id(child) or parent == child):
        return None
    try:
        path = Path(workspace)
        resolved = path.resolve(strict=True)
        if not path.is_absolute() or path != resolved or not resolved.is_dir() or len(str(resolved)) > 4096:
            return None
        return owner, parent, child, str(resolved)
    except (OSError, TypeError, ValueError, RuntimeError):
        return None


def _scope_matches(store, key):
    try:
        owner, parent, child, workspace = key
        if not isinstance(store, ScopedSessionStore) or store.user_id != owner:
            return False
        p, c = store.get(parent), store.get(child)
        if not p or not c or p.id != parent or c.id != child or p.owner_id != owner or c.owner_id != owner:
            return False
        return all(str(store.workspace(sid).resolve(strict=True)) == workspace for sid in (parent, child))
    except Exception:
        return False


@contextmanager
def track_shared_child(store, parent, child, workspace):
    """Issue only from trusted child creation flow; remove on scope exit.

    A false yield means no record was issued, not that hook execution was gated.
    """
    key = _key(getattr(store, 'user_id', None), parent, child, workspace)
    token = object()
    registered = False
    if key is not None and _scope_matches(store, key):
        with _lock:
            # One active parent per child and owner; never replace an existing
            # record, including one bound to a different workspace.
            if len(_active) < _LIMIT and not any(k[0] == key[0] and k[2] == key[2] for k in _active):
                _active[key] = (token, store)
                registered = True
    try:
        yield registered
    finally:
        if registered:
            with _lock:
                current = _active.get(key)
                if current and current[0] is token:
                    del _active[key]


def is_shared_child(owner, parent, child, workspace):
    """Revalidate active relationship and current scoped workspaces.

    A successful result is transient and depends on trusted issuance and storage.
    """
    key = _key(owner, parent, child, workspace)
    if key is None:
        return False
    with _lock:
        current = _active.get(key)
    if current is None or not _scope_matches(current[1], key):
        return False
    with _lock:
        return _active.get(key) is current


def parent_for_shared_child(owner, child, workspace):
    """Find an active parent from internal records, not a caller's parent ID."""
    if not isinstance(owner, str) or not owner.strip() or not _valid_session_id(child):
        return None
    try:
        path = Path(workspace)
        resolved = path.resolve(strict=True)
        if not path.is_absolute() or path != resolved or not resolved.is_dir():
            return None
    except (OSError, TypeError, ValueError, RuntimeError):
        return None
    with _lock:
        matches = [key for key in _active if key[0] == owner and key[2] == child and key[3] == str(resolved)]
    if len(matches) != 1:
        return None
    parent = matches[0][1]
    return parent if is_shared_child(owner, parent, child, resolved) else None
