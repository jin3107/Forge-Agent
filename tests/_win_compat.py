"""Small helpers that keep the test-suite portable to Windows.

Imported by name from the test modules (pytest puts ``tests/`` on ``sys.path``).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def overwrite_file(path, text: str) -> None:
    """Replace the content of ``path``, also when it carries the Windows Hidden attribute.

    ``git worktree add`` marks a worktree's ``.git`` pointer file as Hidden on Windows.
    ``Path.write_text`` opens with CREATE_ALWAYS, which Windows refuses for hidden files
    (PermissionError). Opening an existing file ``r+`` and truncating works everywhere.
    """
    p = Path(path)
    if p.exists():
        with open(p, "r+", encoding="utf-8", newline="") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write(text)
    else:
        p.write_text(text, encoding="utf-8")


def can_create_symlinks() -> bool:
    """True when this process may create symlinks (Windows needs Developer Mode or admin)."""
    if not hasattr(os, "symlink"):
        return False
    if os.name != "nt":
        return True
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.write_text("x")
        try:
            os.symlink(target, Path(tmp) / "link")
        except OSError:
            return False
    return True


def newline_in_filenames_supported() -> bool:
    """Windows cannot create files or directories whose name contains a newline."""
    return sys.platform != "win32"
