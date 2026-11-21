"""Experimental SQLite binding registry, not connected to execution.

The database path and every writer must be trusted by the application. This
registry does not authenticate its caller, its records, the OS user, or the
underlying filesystem. It cannot by itself establish original parentage or
provide authorization. SQLite calls are synchronous and unsuitable for direct
use on a busy event loop without an integration design.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote
from .worktree_binding import Binding
from .worktree_base import _CHILD, _OID


class RegistryError(Exception):
    """Registry could not be safely opened; no data were intentionally deleted."""


def _valid(binding) -> bool:
    if not isinstance(binding, Binding):
        return False
    owner, parent, child = binding.owner_id, binding.parent_sid, binding.child_sid
    ws, key, commit = binding.parent_workspace, binding.repo_key, binding.base_commit
    return (isinstance(owner, str) and 0 < len(owner) <= 256 and
            not any(ord(ch) < 32 or ord(ch) == 127 for ch in owner) and
            isinstance(parent, str) and bool(_CHILD.fullmatch(parent)) and
            isinstance(child, str) and bool(_CHILD.fullmatch(child)) and parent != child and
            isinstance(ws, str) and 0 < len(ws) <= 4096 and '\x00' not in ws and Path(ws).is_absolute() and
            isinstance(key, str) and key == f'wtbase-{child}' and
            isinstance(commit, str) and bool(_OID.fullmatch(commit)))


class BindingRegistry:
    """One immutable record per child. Lookup scope is filtering, not authentication.

    Pass a trusted absolute database path in an existing, protected directory.
    File replacement, malicious writers and privileged OS users are outside the
    trust model. No automatic migration, deletion or recovery is implemented.
    """
    _columns = ('child_sid', 'owner_id', 'parent_sid', 'parent_workspace', 'repo_key', 'base_commit')

    def __init__(self, db_path):
        try:
            self.path = Path(db_path)
            if not self.path.is_absolute() or self.path.is_symlink() or not self.path.parent.is_dir():
                raise ValueError('invalid database path')
            created = False
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(self.path, flags, 0o600)
                os.close(fd)
                created = True
            except FileExistsError:
                pass
            db = self._connect_raw()
            try:
                if created:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('CREATE TABLE bindings (child_sid TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, parent_sid TEXT NOT NULL, parent_workspace TEXT NOT NULL, repo_key TEXT NOT NULL, base_commit TEXT NOT NULL)')
                    db.execute('PRAGMA user_version=1')
                    db.commit()
                self._check_schema(db)
            finally:
                db.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise RegistryError('Binding registry unavailable') from exc

    def _connect_raw(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('invalid database file')
        uri = 'file:' + quote(str(self.path), safe='/:') + '?mode=rw'
        return sqlite3.connect(uri, uri=True, timeout=2)

    def _check_schema(self, db):
        if db.execute('PRAGMA user_version').fetchone() != (1,):
            raise ValueError('unsupported registry version')
        cols = db.execute('PRAGMA table_info(bindings)').fetchall()
        if tuple(row[1] for row in cols) != self._columns or not cols[0][5]:
            raise ValueError('invalid registry schema')

    def record(self, binding) -> str:
        """Trusted issuer only. created/existing/conflict/invalid/error."""
        if not _valid(binding):
            return 'invalid'
        try:
            db = self._connect_raw()
            try:
                self._check_schema(db)
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT child_sid, owner_id, parent_sid, parent_workspace, repo_key, base_commit FROM bindings WHERE child_sid=?', (binding.child_sid,)).fetchone()
                values = (binding.child_sid, binding.owner_id, binding.parent_sid, binding.parent_workspace, binding.repo_key, binding.base_commit)
                if row is not None:
                    db.rollback()
                    return 'existing' if row == values else 'conflict'
                db.execute('INSERT INTO bindings VALUES (?, ?, ?, ?, ?, ?)', values)
                db.commit()
                return 'created'
            finally:
                db.close()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return 'error'

    def lookup(self, owner_id, parent_sid, child_sid) -> tuple[str, Binding | None]:
        """Return found/missing/error; caller must authenticate the scope separately."""
        if (not isinstance(owner_id, str) or not isinstance(parent_sid, str) or
                not isinstance(child_sid, str) or len(owner_id) > 256 or
                not _CHILD.fullmatch(parent_sid) or not _CHILD.fullmatch(child_sid)):
            return ('error', None)
        try:
            db = self._connect_raw()
            try:
                self._check_schema(db)
                row = db.execute('SELECT owner_id, parent_sid, child_sid, parent_workspace, repo_key, base_commit FROM bindings WHERE owner_id=? AND parent_sid=? AND child_sid=?', (owner_id, parent_sid, child_sid)).fetchone()
            finally:
                db.close()
            if row is None:
                return ('missing', None)
            binding = Binding(*row)
            return ('found', binding) if _valid(binding) else ('error', None)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return ('error', None)
