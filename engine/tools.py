"""Built-in agent tools: file operations + shell, confined to a session workspace."""
import asyncio
import codecs
import logging
import os
import signal
from pathlib import Path

from .secrets import sanitized_env

MAX_READ = 60_000          # chars returned to the model
MAX_CMD_OUTPUT = 12_000    # bytes of foreground shell output retained
MAX_FOREGROUND_RUNNING = 8 # per-process, single-event-loop foreground admission limit
MAX_FETCH_BYTES = 128_000  # uncompressed response bytes retained before text extraction
LEGACY_HOOK_TIMEOUT = 30.0  # seconds; does not guarantee process-tree cleanup
FETCH_TOTAL_TIMEOUT = 45.0  # seconds across DNS, redirects and response streaming
MAX_SEARCH_MATCHES = 100
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
             "dist", "build", ".next", ".mypy_cache", ".pytest_cache", "target"}

# ---------------------------------------------------------------------------
# OpenAI-style tool schema (sent to the LLM)
# ---------------------------------------------------------------------------
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Giao việc cho MỘT sub-agent có ngữ cảnh hoàn toàn riêng "
                           "(workspace vẫn dùng chung với bạn) rồi nhận báo cáo tóm tắt. "
                           "Dùng cho: tìm kiếm/đọc hiểu diện rộng, review một vùng code, "
                           "làm nhiệm vụ phụ để giữ context chính gọn. Nhiệm vụ phải tự chứa "
                           "(mục tiêu + tiêu chí xong + format kết quả). Chọn model theo việc: "
                           "ĐỌC/TÌM KIẾM/TÓM TẮT → model 'cheap' · CODE THÔNG THƯỜNG → bỏ trống "
                           "(inherit) · KIẾN TRÚC/AUDIT RỦI RO CAO → 'strong'. Nhiều việc ĐỘC "
                           "LẬP → gọi nhiều spawn_subagent trong cùng một lượt để chạy song song "
                           "(tối đa 6 sub-agent/lượt).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Nhiệm vụ tự chứa cho sub-agent"},
                    "isolation": {"type": "string", "enum": ["shared", "worktree"],
                                  "description": "worktree = sub-agent làm việc trong BẢN SAO "
                                                 "git-worktree tách từ checkpoint — sửa/xoá KHÔNG "
                                                 "ảnh hưởng workspace của bạn; gộp về bằng "
                                                 "/worktree merge, bỏ bằng /worktree discard"},
                    "model": {"type": "string",
                              "description": "Model cho sub-agent. Alias: 'inherit' (mặc định, "
                                             "dùng model hiện tại) · 'cheap' (model rẻ/miễn phí — "
                                             "đọc/tìm kiếm/tóm tắt) · 'strong' (model mạnh nhất — "
                                             "kiến trúc, audit rủi ro cao) · hoặc ID đầy đủ kiểu "
                                             "'vendor/model-name'."},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Lưu kiến thức dài hạn vào MEMORY.md của workspace (TỰ ĐỘNG nạp "
                           "vào các phiên sau). Dùng ngay khi phát hiện: quyết định kiến trúc, "
                           "sở thích/lưu ý của người dùng, lỗi đã gặp và cách khắc phục, "
                           "lệnh build/test chạy được. Ngắn gọn, một dòng một ý.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Ý cần nhớ (≤500 ký tự, markdown)"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_diagnostics",
            "description": "Kiểm tra lỗi (LSP diagnostics: type, undefined name, cú pháp…) "
                           "của MỘT file code bất kỳ — read-only. Dùng sau khi sửa nhiều file "
                           "liên quan hoặc trước khi báo hoàn thành.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Đường dẫn file (relative)"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_symbols",
            "description": "Liệt kê cấu trúc file code qua LSP (documentSymbol): hàm, lớp, "
                           "phương thức kèm số dòng — read-only. Dùng để định vị nhanh trước "
                           "khi edit thay vì đọc cả file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Đường dẫn file (relative)"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_definition",
            "description": "Tìm nơi ĐỊNH NGHĨA một symbol qua LSP — chính xác tuyệt đối "
                           "(khác grep). Cần path + dòng chứa cách gọi của symbol (1-based). "
                           "Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "number", "description": "Dòng có symbol (1-based)"},
                    "character": {"type": "number", "description": "Cột (1-based, mặc định 1)"},
                },
                "required": ["path", "line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_references",
            "description": "Tìm TẤT CẢ nơi dùng một symbol qua LSP (kể cả khai báo) — dùng "
                           "TRƯỚC khi đổi signature/rename để biết mọi call site. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "number"},
                    "character": {"type": "number"},
                },
                "required": ["path", "line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_search_symbols",
            "description": "Tìm class/hàm/biến theo tên trong TOÀN workspace qua LSP "
                           "(workspace/symbol) — nhanh và chính xác hơn grep khi tìm định nghĩa.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Tên symbol, vd 'UserService'"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in the workspace. Use it to explore "
                           "the project before reading files. Paths are relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative dir path, default '.'"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text content of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Gửi tin nhắn sang MỘT phiên khác (vd phiên /loop đang chạy nền, "
                           "phiên fork). Phiên đích sẽ thấy gợi ý INBOX ở lượt kế tiếp của nó "
                           "và đọc bằng check_inbox. Dùng để điều phối: báo kết quả, đổi nhiệm "
                           "vụ, cảnh báo. Không gửi cho chính phiên hiện tại.",
            "parameters": {"type": "object",
                           "properties": {
                               "to_session": {"type": "string",
                                              "description": "ID phiên đích (vd 20261105-ab12cd34)"},
                               "text": {"type": "string", "description": "Nội dung (≤4000 ký tự)"},
                           },
                           "required": ["to_session", "text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_inbox",
            "description": "Đọc tin nhắn CHƯA ĐỌC từ phiên khác gửi tới (tự đánh dấu đã đọc). "
                           "Gọi khi system prompt báo INBOX có tin.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or fully overwrite an existing one. "
                           "Parent folders are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact snippet inside an existing file. "
                           "old_text must match exactly (including whitespace).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Grep-like text search across all text files in the workspace "
                           "(case-insensitive). Returns path:line matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional sub-path to limit search"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Tải nội dung 1 URL (docs, API reference, trang web) về dạng CHỮ "
                           "để bạn đọc. Chỉ http/https, tự strip HTML, cap 20KB. "
                           "Bị chặn với host nội bộ/localhost (chống SSRF).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL đầy đủ bắt đầu bằng http(s)://"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_background",
            "description": "Chạy lệnh shell trong NỀN (dev server, test suite dài, build) — "
                           "KHÔNG block phiên: bạn làm việc khác tiếp, dùng check_background "
                           "để xem output mới và kill_background để dừng.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Lệnh cần chạy nền"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_background",
            "description": "Xem trạng thái + OUTPUT MỚI của tác vụ nền (từ lần xem trước). "
                           "Bỏ trống id → liệt kê tất cả tác vụ nền. Gọi định kỳ để theo dõi "
                           "tiến độ, lệnh xong sẽ có exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "(tuỳ chọn) task id trả về bởi run_background"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_background",
            "description": "Dừng (kill) một tác vụ nền đang chạy theo task id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "task id trả về bởi run_background"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repo_map",
            "description": "Bản đồ workspace nén gọn (A5, kiểu Aider): cây file + symbol "
                           "chính theo ngôn ngữ, rank theo số tham chiếu chéo. Dùng để định "
                           "hướng nhanh trước khi đọc file rời rạc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "number", "description": "(tuỳ chọn) giới hạn ký tự, mặc định 6000"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_background_agent",
            "description": "Giao việc cho sub-agent CHẠY NỀN (A4): trả ngay, lead làm tiếp; "
                           "khi xong tóm tắt TỰ ĐỘNG vào hộp thư phiên này (như tin nhắn) — "
                           "dùng check_background_agents để theo dõi. Chỉ agent mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Việc cần sub-agent tự làm hết"},
                    "model": {"type": "string", "description": "(tuỳ chọn) model phụ — 'cheap'/'strong'/tên"},
                    "isolation": {"type": "string", "enum": ["shared", "worktree"],
                                  "description": "worktree = nhánh git riêng, chưa tự merge"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_background_agents",
            "description": "Xem trạng thái các bg-agent của phiên (running/done/error + tóm tắt). "
                           "Registry sống trong tiến trình — restart là sạch.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "GHI LẠI danh sách việc của nhiệm vụ hiện tại (checklist người dùng "
                           "nhìn thấy tiến độ realtime). Gọi ĐẦU nhiệm vụ để lập kế hoạch, rồi "
                           "cập nhật mỗi khi trạng thái 1 việc đổi (pending → doing → done). "
                           "Luôn gửi DANH SÁCH ĐẦY ĐỦ (mục cũ + mới), tối đa 50 mục.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Danh sách đầy đủ; mỗi mục {'text', 'status'} với "
                                       "status: pending|doing|done",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "status": {"type": "string",
                                           "enum": ["pending", "doing", "done"]},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command (bash) inside the workspace and return "
                           "combined stdout/stderr plus exit code. Use for builds, tests, "
                           "git, running scripts, installing deps, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "description": "Max seconds (default 30, max 120)"},
                },
                "required": ["command"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve(workspace: Path, rel: str) -> Path:
    """Resolve a user/LLM path and ensure it stays inside the workspace."""
    rel = (rel or ".").strip()
    p = (workspace / rel).resolve()
    root = workspace.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"Path '{rel}' is outside the workspace")
    return p


def _memory_save(workspace: Path, args: dict) -> str:
    """Append một ý ghi nhớ vào <ws>/MEMORY.md (cap 8KB — cắt phần cũ)."""
    from .memory import append_entry
    return append_entry(workspace, str(args.get("content", "")))


def _iter_files(base: Path):
    root = base.resolve()

    def contained(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.resolve().is_relative_to(root)
        except (OSError, RuntimeError):
            return False

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in SKIP_DIRS and not d.startswith(".")
                       and contained(Path(dirpath) / d)]
        for f in sorted(filenames):
            path = Path(dirpath) / f
            if f.startswith(".") or not contained(path):
                continue
            yield path


# ---------------------------------------------------------------------------
# Tool implementations (all sync + one async for shell)
# ---------------------------------------------------------------------------
def _list_dir(workspace: Path, args: dict) -> str:
    base = _resolve(workspace, args.get("path", "."))
    if not base.exists():
        return f"Error: path not found: {args.get('path')}"
    if base.is_file():
        return f"{args.get('path')} (file)"
    lines: list[str] = []
    count = 0
    for f in _iter_files(base):
        rel = f.relative_to(base)
        depth = len(rel.parts) - 1
        if depth > 3:
            continue
        lines.append("  " * depth + ("📄 " if True else "") + str(rel))
        count += 1
        if count >= 400:
            lines.append("… (truncated at 400 entries)")
            break
    if not lines:
        return "(empty workspace)"
    return "\n".join(lines)


def _read_file(workspace: Path, args: dict) -> str:
    p = _resolve(workspace, args["path"])
    if not p.exists() or not p.is_file():
        return f"Error: file not found: {args['path']}"
    raw = p.read_bytes()
    if b"\x00" in raw[:2000]:
        return f"Error: '{args['path']}' looks binary; cannot display."
    text = raw.decode("utf-8", "replace")
    if len(text) > MAX_READ:
        return text[:MAX_READ] + f"\n… (truncated, {len(text) - MAX_READ} more chars)"
    return text if text else "(empty file)"


def _make_diff(path: str, old: str, new: str) -> str | None:
    import difflib
    if old == new:
        return None
    d = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    text = "".join(d)
    if len(text) > 8000:
        text = text[:8000] + "\n… (diff quá dài, đã cắt bớt)"
    return text


def _write_file(workspace: Path, args: dict):
    p = _resolve(workspace, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
    p.write_text(args.get("content", ""), encoding="utf-8")
    diff = _make_diff(args["path"], old, args.get("content", "")) if existed else None
    note = f"Wrote {len(args.get('content', ''))} chars to {args['path']}"
    if not existed:
        note += " (file mới)"
    return {"output": note, "diff": diff, "path": args["path"]}


def _edit_file(workspace: Path, args: dict):
    p = _resolve(workspace, args["path"])
    if not p.exists() or not p.is_file():
        return f"Error: file not found: {args['path']}"
    text = p.read_text(encoding="utf-8", errors="replace")
    old, new = args["old_text"], args["new_text"]
    n = text.count(old)
    if n == 0:
        return ("Error: old_text not found in file. Make sure it matches exactly "
                "(whitespace/newlines). Read the file again first.")
    if args.get("replace_all"):
        text2 = text.replace(old, new)
        count = n
    else:
        text2 = text.replace(old, new, 1)
        count = 1
    p.write_text(text2, encoding="utf-8")
    diff = _make_diff(args["path"], text, text2)
    return {"output": f"Edited {args['path']} ({count} occurrence replaced)",
            "diff": diff, "path": args["path"]}


def _search_files(workspace: Path, args: dict) -> str:
    query = args["query"].lower()
    base = _resolve(workspace, args.get("path", "."))
    matches: list[str] = []
    for f in _iter_files(base if base.is_dir() else base.parent):
        try:
            if f.stat().st_size > 1_000_000:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if query in line.lower():
                    rel = f.relative_to(workspace)
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        matches.append(f"… (stopped at {MAX_SEARCH_MATCHES} matches)")
                        return "\n".join(matches)
        except Exception:  # noqa: BLE001, S112 — search không được chết vì 1 file lỗi
            continue
    return "\n".join(matches) if matches else f"No matches for '{args['query']}'"


_FAIL_STREAK: dict[tuple[str, str], int] = {}
# The registry and admission counter are used from one event loop per process.
_FOREGROUND_INFLIGHT = 0
_FOREGROUND_START_CLEANUPS: set[asyncio.Task] = set()
FOREGROUND_CLEANUP_SHUTDOWN_WAIT = 5.0


def _signal_foreground(proc) -> bool:
    """Signal the isolated group where possible; return whether cleanup is uncertain."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return False
        except ProcessLookupError:
            return False
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return True  # Only the direct process was signaled, if still present.
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return False


async def _cleanup_cancelled_foreground_start(spawn: asyncio.Task) -> None:
    """Account for a process returned after spawn cancellation or deadline."""
    try:
        try:
            proc = await spawn
        except Exception:
            return  # No process object was returned by creation.
        if _signal_foreground(proc):
            logging.getLogger(__name__).warning("Foreground spawn cleanup only signaled the direct process")

        async def drain_and_wait():
            if proc.stdout is not None:
                while await proc.stdout.read(16_384):
                    pass
            await proc.wait()

        try:
            await asyncio.wait_for(drain_and_wait(), timeout=1)
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning("Foreground spawn cleanup did not finish within one second")
    except asyncio.CancelledError:
        logging.getLogger(__name__).warning("Foreground spawn cleanup was cancelled")
        raise
    except Exception:
        logging.getLogger(__name__).exception("Foreground spawn cleanup failed")


class _ForegroundLease:
    """One admission slot; late-spawn cleanup can take over its release."""
    def __init__(self) -> None:
        self.transferred = False
        self.released = False

    def release(self) -> None:
        global _FOREGROUND_INFLIGHT
        if not self.released:
            self.released = True
            _FOREGROUND_INFLIGHT -= 1

    def transfer_to(self, cleanup: asyncio.Task) -> None:
        """Retain admission until an independently tracked cleanup settles."""
        self.transferred = True
        cleanup.add_done_callback(lambda _task: self.release())


def _track_foreground_start_cleanup(spawn: asyncio.Task, lease: _ForegroundLease) -> None:
    async def cleanup_and_release():
        try:
            await _cleanup_cancelled_foreground_start(spawn)
        finally:
            lease.release()

    cleanup = asyncio.create_task(cleanup_and_release())
    _FOREGROUND_START_CLEANUPS.add(cleanup)
    cleanup.add_done_callback(_FOREGROUND_START_CLEANUPS.discard)
    lease.transferred = True


async def wait_foreground_start_cleanups() -> int:
    """Wait briefly for already-tracked late-spawn cleanup, without cancelling it."""
    pending = set(_FOREGROUND_START_CLEANUPS)
    if not pending:
        return 0
    _, pending = await asyncio.wait(pending, timeout=FOREGROUND_CLEANUP_SHUTDOWN_WAIT)
    return len(pending)


def _prepare_command(cmd: str, workspace: Path, override: bool = False) -> tuple[str | None, list[str] | None]:
    """Enforce the same shell policy for foreground and background execution.

    Return (blocking message, Docker argv); argv is None for host execution.
    `override` is supplied by a trusted caller after user approval, not tool arguments.
    """
    from . import config, guard
    cfg = config.load_config()
    mode = cfg.get("approval_mode", "guarded")
    verdict = guard.assess_command(cmd)
    if not override and mode == "ask" and verdict["level"] in ("warn", "danger"):
        return "BLOCKED: interactive approval required for this command.", None
    if not override and verdict["level"] == "danger" and mode != "auto":
        return guard.guard_message(cmd, verdict), None
    if cfg.get("docker_sandbox"):
        if not guard.DOCKER_AVAILABLE:
            return "BLOCKED: Docker sandbox is enabled but Docker is unavailable.", None
        return None, guard.docker_command(cmd, workspace)
    return None, None


async def _run_command_result(workspace: Path, args: dict, override: bool = False,
                              *, checkpoint: bool = False, checkpoint_outcome: dict | None = None) -> dict:
    """Reserve a local foreground slot before checkpoint and subprocess work."""
    global _FOREGROUND_INFLIGHT
    if _FOREGROUND_INFLIGHT >= MAX_FOREGROUND_RUNNING:
        return {"ok": False, "output": "BLOCKED: foreground command limit reached; try again later.",
                "exit_code": None, "timed_out": False, "blocked": True, "truncated": False}
    _FOREGROUND_INFLIGHT += 1
    lease = _ForegroundLease()
    try:
        if checkpoint:
            from . import checkpoints
            if checkpoint_outcome is not None:
                checkpoint_outcome["status"] = "failed"
            await checkpoints.snapshot(checkpoints.key_for(workspace), workspace,
                                       f"run_command: {args.get('command', '')[:60]}",
                                       on_detached_cleanup=lease.transfer_to,
                                       outcome=checkpoint_outcome)
        return await _run_command_reserved(workspace, args, override, lease)
    finally:
        if not lease.transferred:
            lease.release()


async def _run_command_reserved(workspace: Path, args: dict, override: bool, lease: _ForegroundLease) -> dict:
    """Run a shell command and return status separately from human-readable output."""
    cmd = args["command"]
    blocked, exec_args = _prepare_command(cmd, workspace, override)
    if blocked:
        return {"ok": False, "output": blocked, "exit_code": None,
                "timed_out": False, "blocked": True, "truncated": False}
    timeout = min(int(args.get("timeout_sec") or 30), 120)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    env = sanitized_env({"TERM": "dumb", "NO_COLOR": "1"})
    # A fresh POSIX session gives the command its own process group, so timeout
    # cleanup cannot signal Forge's process group. Windows retains direct kill.
    session_options = {"start_new_session": True} if os.name == "posix" else {}
    try:
        if exec_args is not None:
            creation = asyncio.create_subprocess_exec(
                *exec_args, cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env, **session_options)
        else:
            creation = asyncio.create_subprocess_shell(
                cmd, cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env, **session_options)
        spawn = asyncio.create_task(creation)
        try:
            # asyncio.wait does not cancel the spawn task on timeout or caller
            # cancellation; distinguish a spawn exception from deadline expiry.
            done, _ = await asyncio.wait({spawn}, timeout=max(0, deadline - loop.time()))
        except asyncio.CancelledError:
            _track_foreground_start_cleanup(spawn, lease)
            raise
        if not done:
            _track_foreground_start_cleanup(spawn, lease)
            return {"ok": False, "output": (f"Error: command timed out after {timeout}s during process creation: {cmd}"
                                               "\nOutput unavailable; cleanup continues if creation completes."),
                    "exit_code": None, "timed_out": True, "blocked": False, "truncated": False}
        proc = spawn.result()
        retained = bytearray()
        truncated = False

        async def drain_output() -> None:
            nonlocal truncated
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(16_384)
                if not chunk:
                    break
                available = max(0, MAX_CMD_OUTPUT - len(retained))
                retained.extend(chunk[:available])
                if len(chunk) > available:
                    truncated = True
            await proc.wait()

        def decoded_output() -> str:
            # Do not introduce a replacement character just because the byte cap
            # splits a UTF-8 sequence. Invalid bytes elsewhere remain visible.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            return decoder.decode(bytes(retained), final=not truncated)

        async def stop_process() -> None:
            if _signal_foreground(proc):
                logging.getLogger(__name__).warning("Foreground cleanup only signaled the direct process")
            # Escaped descendants may hold the pipe open; keep cleanup bounded.
            try:
                await asyncio.wait_for(drain_output(), timeout=1)
            except asyncio.TimeoutError:
                pass

        try:
            await asyncio.wait_for(drain_output(), timeout=max(0, deadline - loop.time()))
        except asyncio.TimeoutError:
            await stop_process()
            output = f"Error: command timed out after {timeout}s: {cmd}"
            if retained:
                output += "\n--- partial output ---\n" + decoded_output()
            if truncated:
                output += "\n… (truncated)"
            return {"ok": False, "output": output, "exit_code": None,
                    "timed_out": True, "blocked": False, "truncated": truncated}
        except asyncio.CancelledError:
            await stop_process()
            raise
        text = decoded_output()
        if truncated:
            text += f"\n… (truncated, exit={proc.returncode})"
        body = f"exit={proc.returncode}\n{text}" if text.strip() else f"exit={proc.returncode} (no output)"
        # --- anti-loop: cảnh báo khi lệnh giống nhau thất bại nhiều lần ---
        key = (str(workspace), cmd)
        if proc.returncode == 0:
            _FAIL_STREAK.pop(key, None)
        else:
            n = _FAIL_STREAK.get(key, 0) + 1
            _FAIL_STREAK[key] = n
            if n >= 2:
                body += (
                    f"\n\n⚠ This exact command has already failed {n} times in a row. "
                    "DO NOT run it again unchanged. Read the error carefully, verify with "
                    "list_dir/read_file, and use a DIFFERENT approach (e.g. on Windows use "
                    "`python` not `python3`, use dir/type instead of ls/cat)."
                )
        return {"ok": proc.returncode == 0, "output": body, "exit_code": proc.returncode,
                "timed_out": False, "blocked": False, "truncated": truncated}
    except Exception as e:  # noqa: BLE001 — lỗi process/OS phải thành output, không nổ tool call
        return {"ok": False, "output": f"Error running command: {e}", "exit_code": None,
                "timed_out": False, "blocked": False, "truncated": False}


async def _run_command(workspace: Path, args: dict, override: bool = False) -> str:
    """Compatibility wrapper for callers that only need the text output."""
    return (await _run_command_result(workspace, args, override=override))["output"]


_TODO_MARK = {"pending": "◻", "doing": "▶", "done": "✅"}


def _todo_write(workspace: Path, args: dict) -> str:
    """Ghi checklist nhiệm vụ vào <ws>/.forge/todo.json (thay toàn bộ danh sách)."""
    import json as _json
    raw = args.get("items")
    if not isinstance(raw, list) or not raw:
        return "Error: thiếu items — gửi danh sách [{text, status}] ĐẦY ĐỦ"
    norm: list[dict] = []
    for it in raw[:50]:
        if isinstance(it, str):
            if it.strip():
                norm.append({"text": it.strip(), "status": "pending"})
        elif isinstance(it, dict):
            text = str(it.get("text") or "").strip()
            st = str(it.get("status") or "pending").lower()
            if text:
                norm.append({"text": text,
                             "status": st if st in _TODO_MARK else "pending"})
    if not norm:
        return "Error: không có mục hợp lệ nào"
    d = workspace / ".forge"
    d.mkdir(exist_ok=True)
    (d / "todo.json").write_text(
        _json.dumps(norm, ensure_ascii=False, indent=1), encoding="utf-8")
    done = sum(1 for i in norm if i["status"] == "done")
    body = "\n".join(f"{_TODO_MARK[i['status']]} {i['text']}" for i in norm)
    return f"TODO cập nhật — {done}/{len(norm)} xong:\n{body}"


def _public_addresses(hostname: str, port: int | None = None) -> list[str]:
    """Resolve once and reject the entire answer if any address is non-public."""
    import ipaddress
    import socket

    host = hostname.strip("[]").lower()
    if not host or host in ("localhost", "*") or host.endswith(".localhost"):
        return []
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (not ip.is_global or ip.is_multicast or ip.is_reserved
                    or (getattr(ip, "ipv4_mapped", None) is not None)):
                return []
            address = str(ip)
            if address not in addresses:
                addresses.append(address)
        return addresses
    except (OSError, ValueError, IndexError):
        return []


def _host_is_private(hostname: str) -> bool:
    """True when a host cannot be resolved exclusively to public addresses."""
    return not _public_addresses(hostname)


def _strip_html(html: str) -> str:
    """Extract approximate readable text in one pass; not an HTML sanitizer."""
    import re as _re
    from html import unescape

    hidden_tags = {"script", "style", "noscript", "svg", "head"}
    block_tags = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                  "section", "article", "table"}
    parts: list[str] = []
    # HTML tag names are ASCII-case-insensitive; Unicode lowercasing can expand
    # characters and invalidate offsets (e.g. capital dotted I).
    lower = html.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    i, n = 0, len(html)
    hidden = None
    while i < n:
        if hidden:
            # Search only forward; script content can contain arbitrary '<'.
            close = lower.find("</" + hidden, i)
            if close < 0:
                break
            after = close + 2 + len(hidden)
            if after < n and not (html[after].isspace() or html[after] in ">/"):
                i = after
                continue
            end = html.find(">", after)
            if end < 0:
                break
            parts.append(" ")
            hidden = None
            i = end + 1
            continue
        start = html.find("<", i)
        if start < 0:
            parts.append(html[i:])
            break
        parts.append(html[i:start])
        if html.startswith("<!--", start):
            end = html.find("-->", start + 4)
            if end < 0:
                break
            parts.append(" ")
            i = end + 3
            continue
        # Respect quotes in attributes, scanning each input character at most
        # once even for malformed tags with no closing angle bracket.
        j, quote = start + 1, None
        while j < n:
            char = html[j]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == ">":
                break
            j += 1
        if j == n:
            parts.append(html[start:])
            break
        raw = html[start + 1:j].strip()
        closing = raw.startswith("/")
        tag = raw[1:].lstrip() if closing else raw
        name = (tag.split(None, 1)[0].rstrip("/").lower() if tag else "")
        if not closing and name in hidden_tags and not raw.endswith("/"):
            hidden = name
        elif name == "br" or (closing and name in block_tags):
            parts.append("\n")
        else:
            parts.append(" ")
        i = j + 1

    # Decode each reference exactly once. Normalizing large numeric references
    # avoids Python's integer-string conversion limit on attacker-sized input.
    refs = _re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
    def decode_ref(match):
        token = match.group(0)
        if token.startswith("&#"):
            digits = token[2:].rstrip(";")
            hexadecimal = digits[:1].lower() == "x"
            if hexadecimal:
                digits = digits[1:]
            significant = digits.lstrip("0") or "0"
            if len(significant) > (6 if hexadecimal else 7):
                return "\ufffd"
            token = "&#" + ("x" if hexadecimal else "") + significant + ";"
        return unescape(token)

    s = refs.sub(decode_ref, "".join(parts)).replace("\u00a0", " ")
    s = _re.sub(r"[ \t]+", " ", s)
    s = _re.sub(r"\n\s*\n\s*", "\n\n", s)
    return s.strip()


async def _fetch_url(workspace: Path, args: dict) -> str:
    """Apply one cooperative deadline to DNS, redirects and response streaming."""
    try:
        return await asyncio.wait_for(_fetch_url_inner(workspace, args), timeout=FETCH_TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        return f"Error: không tải được URL: timeout tổng sau {FETCH_TOTAL_TIMEOUT:g}s"


async def _fetch_url_inner(workspace: Path, args: dict) -> str:
    import httpx

    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: thiếu url"
    try:
        current = httpx.URL(url)
        for hop in range(6):  # initial request plus at most five redirects
            if current.scheme not in ("http", "https"):
                return "Error: chỉ hỗ trợ http/https"
            if not current.host or current.userinfo:
                return "Error: URL không hợp lệ hoặc chứa thông tin đăng nhập"
            hostname = current.raw_host.decode("ascii")
            try:
                addresses = await asyncio.wait_for(
                    asyncio.to_thread(_public_addresses, hostname, current.port), timeout=10)
            except asyncio.TimeoutError:
                return "Error: không tải được URL: DNS timeout"
            if not addresses:
                return ("Error: bị chặn vì bảo mật (SSRF) — không được truy cập "
                        "host nội bộ/localhost/metadata hoặc DNS không an toàn")
            # Connect to the validated IP, while retaining the original Host header
            # and TLS server name (HTTPX sni_hostname request extension). Disable
            # environment proxies so they cannot re-resolve the target separately.
            pinned = current.copy_with(host=addresses[0])
            headers = {"Host": current.netloc.decode("ascii"),
                       "Accept-Encoding": "identity",
                       "User-Agent": "forge-agent/0.38 (+self-hosted BYOK)"}
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False,
                    trust_env=False) as client:
                async with client.stream("GET", pinned, headers=headers,
                                         extensions={"sni_hostname": hostname}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        if hop == 5:
                            return "Error: quá nhiều redirect"
                        current = current.join(resp.headers["location"])
                        continue
                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if any(k in ctype for k in ("image/", "video/", "audio/", "font", "octet-stream",
                                                "zip", "pdf")):
                        return f"Error: URL trả về nội dung nhị phân ({ctype}) — không đọc được dạng chữ"
                    # A compressed chunk can expand arbitrarily before HTTPX yields it.
                    # Request identity and fail closed if a server ignores that request.
                    encoding = resp.headers.get("content-encoding", "").strip().lower()
                    if encoding not in ("", "identity"):
                        return f"Error: không hỗ trợ nội dung nén ({encoding})"
                    data = bytearray()
                    input_truncated = False
                    async for chunk in resp.aiter_bytes():
                        available = MAX_FETCH_BYTES - len(data)
                        data.extend(chunk[:available])
                        if len(chunk) > available:
                            input_truncated = True
                            break
                    decoder = codecs.getincrementaldecoder(resp.encoding or "utf-8")(errors="replace")
                    text = decoder.decode(bytes(data), final=not input_truncated)
                    status = resp.status_code
                    break
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError) as e:
        return f"Error: không tải được URL: {e}"
    if "html" in ctype:
        text = _strip_html(text)
    text = text.strip()
    cap = 20000
    if len(text) > cap:
        text = text[:cap] + f"\n… (đã cắt ở {cap} ký tự của phần đã tải)"
    if input_truncated:
        text += f"\n… (đã cắt tại giới hạn tải {MAX_FETCH_BYTES} byte; chưa tải hết trang)"
    return f"[{status}] {current} · {ctype or 'text'}\n\n{text or '(trang trống)'}"


async def _run_background(workspace: Path, args: dict, *, owner_id: str = "",
                          session_id: str = "", override: bool = False) -> str:
    from . import background as bg
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "Error: thiếu command"
    blocked, exec_args = _prepare_command(cmd, workspace, override)
    if blocked:
        return blocked
    tid, msg = await bg.start(cmd, workspace, owner_id=owner_id, session_id=session_id,
                              exec_args=exec_args)
    if not tid:
        return msg
    await asyncio.sleep(0.3)  # cho có output đầu để LLM thấy ngay
    t = bg.get_scoped(tid, workspace, owner_id=owner_id, session_id=session_id)
    return msg + "\n" + (bg.render(t, incremental=False) if t else "(?)")


def _check_background(workspace: Path, args: dict, *, owner_id: str = "",
                      session_id: str = "") -> str:
    from . import background as bg
    tid = str(args.get("id") or "").strip()
    if not tid:
        return bg.summary(workspace, owner_id=owner_id, session_id=session_id)
    t = bg.get_scoped(tid, workspace, owner_id=owner_id, session_id=session_id)
    if not t:
        return "Error: không có tác vụ nền trong phiên này"
    return bg.render(t, incremental=True)


async def _kill_background(workspace: Path, args: dict, *, owner_id: str = "",
                           session_id: str = "") -> tuple[str, bool]:
    from . import background as bg
    return await bg.kill_result(str(args.get("id") or ""), workspace,
                                owner_id=owner_id, session_id=session_id)


_IMPL = {
    "list_dir": _list_dir,
    "todo_write": _todo_write,
    "check_background": _check_background,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "search_files": _search_files,
    "memory_save": _memory_save,
}


async def execute_tool(name: str, args: dict, workspace: Path, override: bool = False,
                       *, owner_id: str = "", session_id: str = "", legacy_hook=None) -> dict:
    """Run one tool. Returns {"ok": bool, "output": str, "diff"?: str, "path"?: str}.

    override=True: user explicitly approved this exact command interactively.
    """
    from . import checkpoints

    diff = None
    changed_path = None
    checkpoint_outcome = {}
    command_metadata = {"exit_code": None, "timed_out": False, "blocked": False, "truncated": False}
    try:
        if name in ("write_file", "edit_file", "memory_save", "run_background"):
            checkpoint_outcome["status"] = "failed"
            await checkpoints.snapshot(
                checkpoints.key_for(workspace), workspace,
                f"{name}: {args.get('path', args.get('command', ''))[:60]}",
                outcome=checkpoint_outcome)
        if name == "run_command":
            command_result = await _run_command_result(workspace, args, override=override, checkpoint=True,
                                                       checkpoint_outcome=checkpoint_outcome)
            output, ok = command_result["output"], command_result["ok"]
            command_metadata = {k: command_result[k] for k in command_metadata}
        elif name == "fetch_url":
            output = await _fetch_url(workspace, args)
            ok = not output.startswith("Error")
        elif name == "run_background":
            output = await _run_background(workspace, args, owner_id=owner_id,
                                           session_id=session_id, override=override)
            ok = not output.startswith(("Error", "BLOCKED"))
        elif name == "check_background":
            output = _check_background(workspace, args, owner_id=owner_id, session_id=session_id)
            ok = not output.startswith("Error")
        elif name == "kill_background":
            output, uncertain = await _kill_background(workspace, args, owner_id=owner_id, session_id=session_id)
            ok = not uncertain
        elif name in ("lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references"):
            output, ok, changed_path = await _run_lsp_tool(name, workspace, args)
        elif name == "lsp_search_symbols":
            output, ok, changed_path = await _run_lsp_search_symbols(workspace, args)
        elif name in _IMPL:
            res = _IMPL[name](workspace, args)
            if isinstance(res, dict):
                output = res["output"]
                diff = res.get("diff")
                changed_path = res.get("path")
            else:
                output = res
            ok = not output.startswith(("Error", "BLOCKED"))
        else:
            output, ok = f"Error: unknown tool '{name}'", False
    except Exception as e:  # noqa: BLE001 — tool lỗi trả text cho LLM, không nổ vòng agent
        output, ok = f"Error: {e}", False

    # ---- LSP diagnostics sau khi ghi/sửa file code (Claude-Code-style) ----
    diags: list[dict] = []
    if ok and changed_path and name in ("write_file", "edit_file"):
        try:
            from . import lsp as _lsp
            p = Path(changed_path) if str(changed_path).startswith("/") else workspace / changed_path
            if p.is_file() and _lsp.available_for(p):
                diags = await _lsp.diagnostics_for(workspace, p)
                if diags:
                    output = output + "\n\n" + _lsp.format_diags(diags)
        except Exception as _e:  # noqa: BLE001 — LSP không bao giờ được làm hỏng tool call
            print(f"[lsp] bỏ qua lỗi: {_e}")

    # ---- hook on_file_change (kiểu Claude Code hooks — chạy formatter/lint tự đặt) ----
    legacy_trust_denied = False
    if ok and changed_path and name in ("write_file", "edit_file") and legacy_hook is not None:
        try:
            extra = await legacy_hook(workspace, session_id, str(changed_path))
            if not isinstance(extra, str):
                raise ValueError('invalid legacy hook result')
            output += extra
        except Exception:
            # File and any earlier hook effects cannot be reversed here.
            legacy_trust_denied = True
            output += ("\n[File-change hook trust was not confirmed; the file operation completed "
                       "and any effects were not rolled back. Review workspace state before retrying.]")
    elif ok and changed_path and name in ("write_file", "edit_file"):
        try:
            from . import config as _cfg_mod
            from . import hooks as _hooks
            hook_cmd = _cfg_mod.load_config().get("on_file_change")
            if hook_cmd and str(hook_cmd).strip():
                env = sanitized_env({
                    "FORGE_CHANGED_FILE": str(changed_path),
                    "FORGE_WORKSPACE": str(workspace)})
                proc = await asyncio.create_subprocess_shell(
                    str(hook_cmd), cwd=str(workspace), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    **({'start_new_session': True} if os.name == 'posix' else {}))
                try:
                    capture = await asyncio.wait_for(_hooks._hook_capture_merged(proc), timeout=LEGACY_HOOK_TIMEOUT)
                    hook_out = _hooks._hook_format_output(capture, 600)
                except asyncio.TimeoutError:
                    await _hooks._stop_hook_process(proc)
                    hook_out, hook_rc = f"timeout {LEGACY_HOOK_TIMEOUT:g}s; process cleanup unconfirmed", 124
                except asyncio.CancelledError:
                    await _hooks._stop_hook_process(proc)
                    raise
                except Exception:
                    await _hooks._stop_hook_process(proc)
                    raise
                else:
                    hook_rc = proc.returncode or 0
                output += (f"\n🔗 Hook on_file_change (exit {hook_rc}): "
                           + (hook_out or "(không output)"))
        except Exception as _e:  # noqa: BLE001 — hook lỗi không được làm hỏng tool call
            output += f"\n🔗 Hook on_file_change lỗi: {_e}"

    checkpoint_status = checkpoint_outcome.get("status")
    if checkpoint_status in ("failed", "disabled", "unavailable"):
        reasons = {"failed": "failed", "disabled": "is disabled", "unavailable": "is unavailable (Git not found)"}
        output += (f"\n\nWarning: checkpoint {reasons[checkpoint_status]}; "
                   "no new snapshot was confirmed. Undo may not restore the state before this operation.")
    result = {"ok": ok, "output": output}
    if legacy_trust_denied:
        result['hook_trust_denied'] = True
    if checkpoint_status is not None:
        result["checkpoint_status"] = checkpoint_status
    if name == "run_command":
        result.update(command_metadata)
    if diff:
        result["diff"] = diff
    if changed_path:
        result["path"] = changed_path
    if diags:
        result["diagnostics"] = diags
    return result


async def _run_lsp_tool(name: str, workspace: Path, args: dict) -> tuple[str, bool, None]:
    """lsp_diagnostics / lsp_symbols — read-only, không bao giờ hỏng tool call."""
    from . import lsp as _lsp
    rel = str(args.get("path", "")).strip()
    if not rel:
        return "Error: thiếu path", False, None
    p = Path(rel) if str(rel).startswith("/") else workspace / rel
    if not p.is_file():
        return f"Error: không tìm thấy file {rel}", False, None
    lang = _lsp.lang_for(p)
    if lang is None:
        return f"Bỏ qua: {p.suffix} không phải file code có language server", True, None
    if not _lsp.server_cmd(lang):
        return (f"LSP cho {lang} chưa cài trên máy. Hướng dẫn: pip install "
                + "python-lsp-server[pyflakes,pycodestyle] (Python) hoặc "
                + "npm i -g typescript typescript-language-server (TS/JS).", True, None)
    if name == "lsp_diagnostics":
        diags = await _lsp.diagnostics_for(workspace, p)
        if not diags:
            return f"✓ {rel}: không có lỗi LSP", True, None
        return _lsp.format_diags(diags).replace("sau khi ghi file", f"({rel})"), True, None
    # các tool còn lại cần session
    sess = await _lsp._get_session(workspace, lang)
    if sess is None:
        return f"Error: không khởi động được language server cho {p.suffix}", False, None
    if name == "lsp_symbols":
        syms = await sess.symbols(p)
        if not syms:
            return "(không có symbol — file rỗng hoặc ngôn ngữ chưa hỗ trợ)", True, None
        lines = [f"{'  ' * x['depth']}{x['kind']} {x['name']} (dòng {x['line']})" for x in syms[:80]]
        return f"Cấu trúc {rel}:\n" + "\n".join(lines), True, None
    if name in ("lsp_definition", "lsp_references"):
        line = int(args.get("line", 1) or 1)
        char = int(args.get("character", 1) or 1)
        try:
            locs = (await sess.definition(p, line, char) if name == "lsp_definition"
                    else await sess.references(p, line, char))
        except Exception as e:  # noqa: BLE001 — LSP không bao giờ được làm hỏng tool call
            return f"Error: LSP {name} lỗi: {e}", False, None
        if not locs:
            return (f"Không tìm thấy {name.replace('lsp_', '')} cho vị trí {rel}:{line} "
                    + "(kiểm tra lại line/cột — dùng lsp_symbols để định vị trước)", True, None)
        ws_str = str(workspace)
        rows = []
        for ln in locs:
            short = ln["file"].replace(ws_str + "/", "").replace(ws_str, ".")
            rows.append(f"  {short}:{ln['line']}:{ln['character']}")
        verb = "Định nghĩa" if name == "lsp_definition" else "Nơi dùng"
        return f"{verb} ({len(locs)} vị trí):\n" + "\n".join(rows), True, None
    return f"Error: unknown lsp tool {name}", False, None


async def _run_lsp_search_symbols(workspace: Path, args: dict) -> tuple[str, bool, None]:
    from . import lsp as _lsp
    q = str(args.get("query", "")).strip()
    if not q:
        return "Error: thiếu query", False, None
    # thử từng ngôn ngữ có language server
    results: list[dict] = []
    no_support: list[str] = []
    for lang, cmds in (("python", _lsp.SERVER_DEFS["python"]), ("typescript", _lsp.SERVER_DEFS["typescript"])):
        if not any(__import__("shutil").which(c[0]) for c in cmds):
            continue
        sess = await _lsp._get_session(workspace, lang)
        if sess is None:
            continue
        if not sess.capabilities.get("workspaceSymbolProvider"):
            no_support.append(lang)
            continue
        try:
            results = await sess.workspace_symbols(q)
            if results:
                break
        except Exception:  # noqa: BLE001, S112 — symbol scan không được chết vì 1 file lỗi
            continue
    if not results:
        if no_support:
            return (f"Language server ({', '.join(no_support)}) không hỗ trợ tìm symbol toàn "
                    + "workspace — dùng tool search_files thay thế", True, None)
        return f"Không tìm thấy symbol nào khớp «{q}» (hoặc máy chưa có language server)", True, None
    ws_str = str(workspace)
    rows = [f"  {x['kind']} {x['name']} — {x['file'].replace(ws_str + '/', '')}:{x['line']}"
            for x in results[:30]]
    return f"Symbols khớp «{q}» ({len(results)}):\n" + "\n".join(rows), True, None
