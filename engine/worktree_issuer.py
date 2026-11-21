"""Experimental worktree issuer. NOT wired to production or an access-control gate.

The caller must authenticate the scoped store and protect registry/database files.
This helper creates the child itself rather than accepting a caller's claimed
parent-child record. Creation, capture, pointer, Git and registry publication
are not atomic and may leave artifacts; nothing here provides OS isolation.
"""
from pathlib import Path
from . import config, worktree, worktree_base
from .session import ScopedSessionStore
from .worktree_binding import Binding, validate
from .worktree_binding_registry import BindingRegistry


async def issue(store, parent_sid: str, registry, *, model: str = '') -> dict:
    """Best-effort experimental child/base/record creation; never starts a child.

    Authenticate caller and scope before calling. A failure may return an owned
    child_sid to aid investigation, but does not remove any partial artifacts.
    A successful record is not a durable audit or a guarantee of later state.
    """
    failed = {'ok': False, 'message': 'Worktree issuance failed; inspect partial artifacts before retrying.'}
    if (not isinstance(store, ScopedSessionStore) or not store.user_id or
            not isinstance(registry, BindingRegistry) or not isinstance(parent_sid, str) or
            not worktree_base._CHILD.fullmatch(parent_sid)):
        return failed
    child_sid = None
    try:
        parent = store.get(parent_sid)
        if not parent or parent.id != parent_sid or parent.owner_id != store.user_id:
            return failed
        parent_ws = Path(store.workspace(parent_sid)).resolve(strict=True)
        if not parent_ws.is_dir():
            return failed
        child = store.create(model=model, title='Worktree sub-agent')
        child_sid = child.id
        failed = {**failed, 'child_sid': child_sid}
        if (not isinstance(child_sid, str) or not worktree_base._CHILD.fullmatch(child_sid) or
                child_sid == parent_sid or child.owner_id != store.user_id or store.get(child_sid) is None):
            return failed
        target = worktree.wt_path(child_sid)
        if target.exists() or target.is_symlink():
            return failed
        captured = await worktree_base.capture(child_sid, parent_ws)
        if not captured.get('ok'):
            return failed
        # Recheck parent before Git worktree creation; this does not close races.
        current = store.get(parent_sid)
        if (not current or current.id != parent_sid or current.owner_id != store.user_id or
                Path(store.workspace(parent_sid)).resolve(strict=True) != parent_ws or
                target.exists() or target.is_symlink()):
            return failed
        wt = await worktree.make(captured['repo_key'], child_sid)
        if not wt or Path(wt).resolve(strict=True) != target.resolve(strict=True):
            return failed
        ptr = config.WORKSPACES_DIR / f'{child_sid}.path'
        # New session: do not overwrite a pointer that appeared during setup.
        with ptr.open('x', encoding='utf-8') as file:
            file.write(str(target.resolve(strict=True)))
        binding = Binding(store.user_id, parent_sid, child_sid, str(parent_ws),
                          captured['repo_key'], captured['commit'])
        if not await validate(store, binding):
            return failed
        if registry.record(binding) != 'created':
            return failed
        # A change after publication is an error; the record remains for manual
        # investigation and must be revalidated by any future consumer.
        if not await validate(store, binding):
            return failed
        return {'ok': True, 'child_sid': child_sid}
    except Exception:
        return failed
