"""Experimental validator for a *trusted* worktree binding, not a server gate.

No trusted binding storage or issuer exists here. A caller that accepts a forged
Binding as authoritative can authorize the wrong relationship. These checks are
best effort and raceable; do not use them alone for access control or cleanup.
"""
from dataclasses import dataclass
from pathlib import Path
from . import worktree, worktree_base
from .session import ScopedSessionStore


@dataclass(frozen=True)
class Binding:
    owner_id: str
    parent_sid: str
    child_sid: str
    parent_workspace: str
    repo_key: str
    base_commit: str


async def validate(store, binding) -> bool:
    """Check a caller-trusted binding against live scoped sessions and Git state.

    Does not authenticate the binding, lock files, attest the workspace contents,
    or authorize a subsequent action. Local unscoped sessions are not supported.
    """
    if not isinstance(store, ScopedSessionStore) or not isinstance(binding, Binding):
        return False
    if (not isinstance(binding.owner_id, str) or not binding.owner_id or
            store.user_id != binding.owner_id or
            not isinstance(binding.parent_sid, str) or not worktree_base._CHILD.fullmatch(binding.parent_sid) or
            not isinstance(binding.child_sid, str) or not worktree_base._CHILD.fullmatch(binding.child_sid) or
            binding.parent_sid == binding.child_sid or
            binding.repo_key != f'wtbase-{binding.child_sid}' or
            not isinstance(binding.base_commit, str) or not worktree_base._OID.fullmatch(binding.base_commit) or
            not isinstance(binding.parent_workspace, str)):
        return False
    try:
        parent = store.get(binding.parent_sid)
        child = store.get(binding.child_sid)
        if (not parent or not child or parent.id != binding.parent_sid or child.id != binding.child_sid or
                parent.owner_id != binding.owner_id or child.owner_id != binding.owner_id):
            return False
        parent_ws = Path(store.workspace(binding.parent_sid)).resolve(strict=True)
        if not parent_ws.is_dir() or str(parent_ws) != binding.parent_workspace:
            return False
        wt = worktree.wt_path(binding.child_sid)
        repo = worktree._repo(binding.repo_key)
        if wt.is_symlink() or repo.is_symlink() or not wt.is_dir() or not repo.is_dir():
            return False
        target = wt.resolve(strict=True)
        if (target == parent_ws or target.is_relative_to(parent_ws) or parent_ws.is_relative_to(target) or
                Path(store.workspace(binding.child_sid)).resolve(strict=True) != target):
            return False
        if not await worktree._worktree_matches(target, repo, binding.child_sid):
            return False
        for ref in ('refs/heads/forge', f'refs/heads/iso/{binding.child_sid}'):
            rc, oid = await worktree._run('--git-dir', str(repo), 'rev-parse', '--verify', ref)
            if rc != 0 or oid != binding.base_commit:
                return False
        return True
    except Exception:
        return False
