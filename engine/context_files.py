# -*- coding: utf-8 -*-
"""Nạp file ngữ cảnh (AGENTS.md, MEMORY.md…) với @import — kiểu CLAUDE.md của Claude Code.

Quy tắc (theo research v0.12):
- Dòng văn xuôi chứa ``@path/to/file`` → thay bằng nội dung file đó.
- Đường dẫn tương đối với thư mục file chứa; chỉ nạp file bên trong root cho phép.
- Mặc định root là thư mục của file đầu tiên; caller có thể chỉ định workspace/data root.
- Đệ quy tối đa 5 tầng; chống vòng lặp (mỗi file nạp 1 lần mỗi nhánh).
- KHÔNG import bên trong code block ``` và inline `code span`.
- Chỉ import khi file TỒN TẠI — nếu không, giữ nguyên chữ (không mất thông tin).
- Mỗi import bị cắt ở 8KB để không phình context.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_DEPTH = 5
PER_IMPORT_CAP = 8000
# @token: không chứa khoảng trắng/backtick/markdown dấu — cắt dấu câu cuối khi xử lý
IMPORT_RE = re.compile(r"@([^\s`*_\[\]<>|]+)")
_TRAIL = ".,;:!?)\u3001\u3002"


def _resolve(token: str, base_dir: Path) -> Path | None:
    tok = token.rstrip(_TRAIL)
    if not tok or tok.startswith(("@", "#")):
        return None
    p = Path(tok).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    try:
        return p.resolve() if p.exists() else None
    except (OSError, RuntimeError):
        return None


def _expand_outside(text: str, base_dir: Path, depth: int, seen: frozenset,
                    root: Path) -> str:
    """Xử lý 1 đoạn NGOÀI code span: thay @token bằng nội dung file (nếu có)."""

    def sub(m: re.Match) -> str:
        target = _resolve(m.group(1), base_dir)
        if target is None:
            return m.group(0)  # file không tồn tại → giữ nguyên
        if not target.is_relative_to(root):
            return "[@import blocked: outside allowed root]"
        return load_with_imports(target, depth=depth + 1, _seen=seen, root=root)

    return IMPORT_RE.sub(sub, text)


def _process_line(line: str, base_dir: Path, depth: int, seen: frozenset, root: Path) -> str:
    """Xử lý 1 dòng: bỏ qua phần nằm trong `inline code span`."""
    parts = line.split("`")
    out: list[str] = []
    for i, seg in enumerate(parts):
        if i % 2 == 0:  # ngoài code span
            if depth >= MAX_DEPTH:
                out.append(seg)
            else:
                out.append(_expand_outside(seg, base_dir, depth, seen, root))
        else:
            out.append(seg)
    return "`".join(out)


def load_with_imports(path: Path, depth: int = 0,
                      _seen: frozenset | None = None, *, root: Path | None = None) -> str:
    """Load a context file and imports confined to the caller's allowed root."""
    path = Path(path)
    seen = _seen if _seen is not None else frozenset()
    try:
        allowed_root = Path(root if root is not None else path.parent).resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(allowed_root):
            return ""
        rp = str(resolved)
    except (OSError, RuntimeError):
        return ""
    if rp in seen:  # vòng lặp → bỏ
        return "[@import bị bỏ qua: vòng lặp]"
    try:
        raw = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if depth >= MAX_DEPTH:
        return raw  # quá sâu: nạp nguyên file này, không nội suy tiếp
    base_dir = path.parent
    child_seen = seen | {rp}  # con cháu không được quay lại file này
    in_fence = False
    out_lines: list[str] = []
    for line in raw.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        out_lines.append(line if in_fence else _process_line(line, base_dir, depth, child_seen, allowed_root))
    text = "\n".join(out_lines)
    if len(text.encode("utf-8")) > PER_IMPORT_CAP and depth > 0:
        data = text.encode("utf-8")[:PER_IMPORT_CAP]
        text = data.decode("utf-8", errors="ignore") + "\n[… @import cắt ở 8KB …]"
    return text
