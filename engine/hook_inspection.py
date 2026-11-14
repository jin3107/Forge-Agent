"""Bounded, side-effect-free workspace hook configuration inspection.

This helper is not yet wired into hook execution and is not an approval gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class Inspection:
    # absent, no_hooks, hooks, or invalid. Invalid is never proof of safety.
    state: str
    digest: str | None = None
    config: dict[str, Any] | None = None
    raw: bytes | None = field(default=None, repr=False)


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate JSON key')
        out[key] = value
    return out


def _invalid_constant(_value):
    raise ValueError('non-finite JSON number')


def _valid_items(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, list):
        return False
    for entry in value:
        if isinstance(entry, str):
            if not entry.strip():
                return False
        elif isinstance(entry, dict):
            if not isinstance(entry.get('command'), str) or not entry['command'].strip():
                return False
            if 'matcher' in entry and not isinstance(entry['matcher'], str):
                return False
        else:
            return False
    return True


def inspect_bytes(raw: bytes | None) -> Inspection:
    """Apply the same bounded grammar and classification to already-read bytes."""
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES:
        return Inspection('invalid')
    try:
        cfg = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                         parse_constant=_invalid_constant)
        if not isinstance(cfg, dict):
            return Inspection('invalid')
        hooks = cfg.get('hooks')
        if hooks is not None:
            if not isinstance(hooks, dict):
                return Inspection('invalid')
            if any(not isinstance(k, str) or not _valid_items(v) for k, v in hooks.items()):
                return Inspection('invalid')
        legacy = cfg.get('on_file_change')
        if legacy is not None and not isinstance(legacy, str):
            return Inspection('invalid')
        # A nonempty mapping overrides global hooks even for an unknown event.
        has_hooks = bool(hooks) or bool(legacy)
        return Inspection('hooks' if has_hooks else 'no_hooks', hashlib.sha256(raw).hexdigest(), cfg, raw)
    except (UnicodeError, ValueError, RecursionError):
        return Inspection('invalid')


def inspect(workspace: Path) -> Inspection:
    """Inspect workspace config without running commands or modifying files.

    The digest binds the exact bytes read, not referenced scripts or later reads.
    Filesystem checks are best effort and do not establish an OS boundary.
    """
    directory = Path(workspace) / '.forge'
    target = directory / 'config.json'
    try:
        try:
            directory_stat = directory.lstat()
        except FileNotFoundError:
            return Inspection('absent')
        if not stat.S_ISDIR(directory_stat.st_mode):
            return Inspection('invalid')
        try:
            before = target.lstat()
        except FileNotFoundError:
            return Inspection('absent')
        if not stat.S_ISREG(before.st_mode):
            return Inspection('invalid')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(target, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return Inspection('invalid')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                raw = stream.read(MAX_CONFIG_BYTES + 1)
        finally:
            if fd != -1:
                os.close(fd)
        return inspect_bytes(raw)
    except (OSError, UnicodeError, ValueError, RecursionError):
        # Do not expose path/exception details; consumers must fail closed.
        return Inspection('invalid')
