# -*- coding: utf-8 -*-
"""Lệnh shell chạy NỀN cho agent (chuẩn background tasks của Claude Code).

- start() → task id; tiến trình chạy độc lập, agent làm việc khác tiếp.
- status()/check() → trạng thái + output MỚI kể từ lần xem trước (offset).
- Buffer giữ đuôi 60KB; nếu output chưa đọc bị bỏ, kết quả báo rõ.
- Mỗi lần render trả tối đa 4KB và giữ lại phần chưa xem cho lần tiếp theo.
- Chỉ sống trong tiến trình hiện tại (không persist — tác vụ nền là việc tức thời).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from pathlib import Path

from . import proctree
from .secrets import sanitized_env

MAX_RUNNING = 8
MAX_TOTAL = 24         # vượt ngưỡng này dọn task đã xong cũ nhất
BUF_CAP = 60_000        # giữ đuôi 60KB
VIEW_CAP = 4000         # mỗi lần check trả tối đa 4KB output mới

_TASKS: dict[str, dict] = {}
# Accessed only on the server's event loop. Reserve before awaiting subprocess
# creation so concurrent starts cannot all pass the quota check.
_STARTING = 0
_STARTS: set[asyncio.Future] = set()
_CANCELLED_START_CLEANUPS: set[asyncio.Task] = set()
# Pumps may outlive a killed task's visible status or its entry in _TASKS.
_PUMPS: set[asyncio.Task] = set()
_SHUTTING_DOWN = False
_SHUTDOWN_EPOCH = 0
SHUTDOWN_START_WAIT = 5.0
SHUTDOWN_PUMP_WAIT = 2.0


def _reap() -> None:
    if len(_TASKS) <= MAX_TOTAL:
        return
    done = [(t["started"], k) for k, t in _TASKS.items() if t["status"] != "running"]
    for _, k in sorted(done)[: len(_TASKS) - MAX_TOTAL]:
        _TASKS.pop(k, None)


def _scope(workspace: Path, owner_id: str = "", session_id: str = "") -> tuple[str, str, str]:
    """Identity supplied by the engine, never by model tool arguments."""
    return (owner_id, session_id, str(Path(workspace).resolve()))


def _register_task(command: str, proc, scope: tuple[str, str, str]) -> dict:
    tid = f"bg-{uuid.uuid4().hex}"
    t = {"id": tid, "cmd": command, "proc": proc, "buf": bytearray(), "off": 0,
         "base_offset": 0, "status": "running", "code": None,
         "started": time.time(), "killed": False, "output_read_failed": False,
         "output_read_done": False, "wait_failed": False, "stop_unconfirmed": False, "scope": scope}
    _TASKS[tid] = t
    pump = asyncio.create_task(_pump(t))
    _PUMPS.add(pump)
    def pump_done(done: asyncio.Task) -> None:
        _PUMPS.discard(done)
        # A task cancelled before its coroutine starts cannot run its handler.
        if done.cancelled() and not t.get("_pump_started", False):
            t["output_read_done"] = True
            t["output_read_failed"] = True
            if t["status"] == "running":
                t["wait_failed"] = True
                t["code"] = None
    pump.add_done_callback(pump_done)
    return t


def _release_start(start_done: asyncio.Future, cleanup_failed: bool) -> None:
    global _STARTING
    if cleanup_failed:
        logging.getLogger(__name__).warning("In-flight background start cleanup incomplete")
    _STARTING -= 1
    _STARTS.discard(start_done)
    if not start_done.done():
        start_done.set_result(cleanup_failed)


async def _cleanup_cancelled_start(spawn: asyncio.Task, command: str,
                                   scope: tuple[str, str, str], start_done: asyncio.Future) -> None:
    """Keep the reservation until a cancelled caller's spawn is accounted for."""
    failed = False
    try:
        try:
            proc = await spawn
        except Exception:
            return  # Spawn failed; no process object to clean up.
        failed = True
        t = _register_task(command, proc, scope)
        _, failed = await _kill_task(t)
    except asyncio.CancelledError:
        failed = True
        raise
    except Exception:
        failed = True
        logging.getLogger(__name__).exception("Cancelled background start cleanup failed")
    finally:
        _release_start(start_done, failed)


async def start(command: str, workspace: Path, *, owner_id: str = "",
                session_id: str = "", exec_args: list[str] | None = None) -> tuple[str, str]:
    """Start a task bound to an owner, session and resolved workspace."""
    global _STARTING
    if _SHUTTING_DOWN:
        return "", "Error: server đang dừng, không thể bắt đầu tác vụ nền."
    epoch = _SHUTDOWN_EPOCH
    _reap()
    running = sum(1 for t in _TASKS.values() if t["status"] == "running")
    if running + _STARTING >= MAX_RUNNING:
        # The limit is process-wide; do not disclose another session's task IDs.
        return "", "Error: đã đạt giới hạn tác vụ nền của server — thử lại sau."
    _STARTING += 1
    start_done = asyncio.get_running_loop().create_future()
    _STARTS.add(start_done)
    cleanup_failed = False
    transferred = False
    try:
        env = sanitized_env({"TERM": "dumb", "NO_COLOR": "1"})
        scope = _scope(workspace, owner_id, session_id)
        # proctree isolates the command: its own process group on POSIX, a Job Object on Windows.
        if exec_args is not None:
            creation = proctree.spawn_exec(
                *exec_args, cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env)
        else:
            creation = proctree.spawn_shell(
                command, cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env)
        spawn = asyncio.create_task(creation)
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The OS process may already exist even though create_subprocess has
            # not returned to us. A separate tracked task takes ownership.
            cleanup = asyncio.create_task(_cleanup_cancelled_start(spawn, command, scope, start_done))
            _CANCELLED_START_CLEANUPS.add(cleanup)
            cleanup.add_done_callback(_CANCELLED_START_CLEANUPS.discard)
            transferred = True
            raise
        t = _register_task(command, proc, scope)
        if epoch != _SHUTDOWN_EPOCH:
            # Shutdown began while process creation was in flight. Do not hand
            # the task to a caller after the server has started stopping.
            cleanup_failed = True
            _, cleanup_failed = await _kill_task(t)
            return "", "Error: server đang dừng, tác vụ nền đã được yêu cầu dừng."
        return t["id"], f"[{t['id']}] đã chạy nền: {command}"
    finally:
        if not transferred:
            _release_start(start_done, cleanup_failed)


async def _pump(t: dict) -> None:
    t["_pump_started"] = True
    try:
        stream = t["proc"].stdout
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            t["buf"].extend(chunk)
            if len(t["buf"]) > BUF_CAP:
                dropped = len(t["buf"]) - BUF_CAP
                del t["buf"][:dropped]
                t["base_offset"] += dropped
    except asyncio.CancelledError:
        # Cancellation interrupts draining; do not block cancellation on wait().
        t["output_read_failed"] = True
        if t["status"] == "running":
            t["wait_failed"] = True
            t["code"] = None
        raise
    except Exception:
        # Keep this visible even while the child is still running. Do not expose
        # exception text, which could contain sensitive command/output data.
        t["output_read_failed"] = True
    finally:
        # Stream completion is independent of process exit or wait outcome.
        t["output_read_done"] = True
    try:
        code = await t["proc"].wait()
        if not isinstance(code, int):
            raise ValueError("process wait returned no exit code")
    except asyncio.CancelledError:
        if t["status"] == "running":
            t["wait_failed"] = True
            t["code"] = None
        raise
    except Exception:
        # A concurrent kill may already have confirmed termination. Otherwise
        # keep the slot and task eligible for shutdown rather than inventing
        # an exit code or claiming the process is gone.
        if t["status"] == "running":
            t["wait_failed"] = True
            t["code"] = None
    else:
        t["wait_failed"] = False
        t["code"] = code
        t["status"] = "killed" if t["killed"] else ("done" if code == 0 else "failed")


def get(tid: str) -> dict | None:
    return _TASKS.get(tid.strip())


def get_scoped(tid: str, workspace: Path, *, owner_id: str = "", session_id: str = "") -> dict | None:
    task = get(tid)
    return task if task and task.get("scope") == _scope(workspace, owner_id, session_id) else None


def render(t: dict, incremental: bool) -> str:
    """Consume at most VIEW_CAP bytes of unread output from the shared task cursor.

    The flag is retained for caller compatibility; both initial and subsequent
    views consume unread output so a view limit cannot silently discard the rest.
    """
    import codecs

    base = t["base_offset"]
    end = base + len(t["buf"])
    lost = max(0, base - t["off"])
    start = max(base, t["off"])
    raw = bytes(t["buf"][start - base:start - base + VIEW_CAP])
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    final = t.get("output_read_done", False) and start + len(raw) == end
    new = decoder.decode(raw, final=final)
    pending = len(decoder.getstate()[0])
    t["off"] = start + len(raw) - pending
    remaining = end - t["off"]
    status_vi = {"running": "⏳ đang chạy", "done": "✅ xong",
                 "failed": f"❌ lỗi (exit {t['code']})",
                 "killed": f"🛑 đã kill (exit {t['code']})"}.get(t["status"], t["status"])
    if t.get("wait_failed"):
        status_vi = "⚠ chưa xác nhận trạng thái tiến trình"
    head = f"[{t['id']}] {status_vi} · {t['cmd'][:80]} · {time.time() - t['started']:.0f}s"
    parts = [head]
    if t.get("wait_failed"):
        parts.append("⚠ exit status could not be confirmed; process may still be running")
    if t.get("stop_unconfirmed"):
        parts.append("⚠ process or group stop could not be confirmed; descendants may still be running")
    if t.get("output_read_failed"):
        parts.append("⚠ output may be incomplete: không đọc được toàn bộ output của tiến trình")
    if lost:
        parts.append(f"⚠ output lost: {lost} byte chưa đọc đã bị loại khỏi buffer")
    if new:
        parts.append(f"--- output mới ---\n{new}")
    elif incremental:
        parts.append("(chưa có output mới)")
    if remaining:
        parts.append(f"… {remaining} byte remaining — gọi check_background để đọc tiếp")
    return "\n".join(parts)


async def kill_result(tid: str, workspace: Path, *, owner_id: str = "", session_id: str = "") -> tuple[str, bool]:
    """Return the scoped stop message and whether cleanup is uncertain."""
    t = get_scoped(tid, workspace, owner_id=owner_id, session_id=session_id)
    if not t:
        return f"Error: không có tác vụ «{tid}»", True
    return await _kill_task(t)


async def kill(tid: str, workspace: Path, *, owner_id: str = "", session_id: str = "") -> str:
    # Preserve the existing string-returning API for callers outside tools.
    message, _ = await kill_result(tid, workspace, owner_id=owner_id, session_id=session_id)
    return message


async def _kill_task(t: dict) -> tuple[str, bool]:
    """Stop a trusted task; return message and whether cleanup is uncertain."""
    if t["status"] != "running":
        if t.get("stop_unconfirmed") and os.name != "posix":
            # The wrapper is gone but an earlier stop left the tree unconfirmed: stop it now.
            leftover = proctree.tree_of(t["proc"])
            if leftover is not None:
                leftover.terminate()
                if await leftover.confirm_gone(5):
                    t["stop_unconfirmed"] = False
        uncertain = bool(t.get("stop_unconfirmed"))
        message = f"[{t['id']}] đã kết thúc trước đó ({t['status']}) — không cần kill."
        if uncertain:
            message += "\n⚠ process or group stop could not be confirmed"
        return message, uncertain
    fallback_warning = "\n⚠ Không thể kill nhóm; chỉ gửi tín hiệu tới tiến trình trực tiếp." if t.get("stop_direct_only") else ""
    if t["killed"] and os.name != "posix":
        # A retry after an unconfirmed stop: signal the job again; terminating twice is harmless.
        retry_tree = proctree.tree_of(t["proc"])
        if retry_tree is not None:
            retry_tree.terminate()
    if not t["killed"]:
        if os.name == "posix":
            try:
                # The group can outlive its leader while descendants hold stdout.
                os.killpg(t["proc"].pid, signal.SIGKILL)
            except ProcessLookupError:
                t["stop_unconfirmed"] = True
                if t["proc"].returncode is not None:
                    return f"Error: [{t['id']}] process group not found; group stop could not be confirmed.", True
                try:
                    t["proc"].kill()
                except OSError:
                    return f"Error: [{t['id']}] process group not found and direct stop failed; group stop could not be confirmed.", True
                fallback_warning = "\n⚠ Không thể kill nhóm; chỉ gửi tín hiệu tới tiến trình trực tiếp."
            except OSError:
                t["stop_unconfirmed"] = True
                try:
                    t["proc"].kill()
                except OSError:
                    # OS exception messages can contain paths or other sensitive data.
                    return f"Error: [{t['id']}] không thể kill nhóm hoặc tiến trình; chưa xác nhận dừng.", True
                fallback_warning = "\n⚠ Không thể kill nhóm; chỉ gửi tín hiệu tới tiến trình trực tiếp."
        else:
            tree = proctree.tree_of(t["proc"])
            try:
                if tree is not None:
                    # Kill the whole Windows job, not just the cmd.exe wrapper.
                    if not tree.terminate():
                        raise OSError
                else:
                    t["proc"].kill()
            except ProcessLookupError:
                t["stop_unconfirmed"] = True
                return f"Error: [{t['id']}] process not found; stop could not be confirmed.", True
            except OSError:
                t["stop_unconfirmed"] = True
                return f"Error: [{t['id']}] không thể kill tiến trình; chưa xác nhận dừng.", True
        t["stop_direct_only"] = bool(fallback_warning)
        t["killed"] = True
    try:
        code = await asyncio.wait_for(t["proc"].wait(), timeout=5)
        if not isinstance(code, int):
            raise ValueError("process wait returned no exit code")
    except asyncio.CancelledError:
        if t["status"] == "running":
            t["stop_unconfirmed"] = True
            t["wait_failed"] = True
            t["code"] = None
        raise
    except Exception:
        # Another stop waiter or the pump may have confirmed exit meanwhile.
        if t["status"] != "running":
            return render(t, incremental=False), bool(t.get("stop_unconfirmed"))
        t["stop_unconfirmed"] = True
        t["wait_failed"] = True
        t["code"] = None
        return f"Error: [{t['id']}] exit status could not be confirmed after stop request; retry stop to wait again." + fallback_warning, True
    tree = proctree.tree_of(t["proc"]) if os.name != "posix" else None
    if tree is not None and not await tree.confirm_gone(5):
        # The wrapper exited but a descendant is still alive: do not report a confirmed stop.
        t["stop_unconfirmed"] = True
        return (f"Error: [{t['id']}] the command tree is still running after the stop request; "
                "retry stop.", True)
    # pump có thể chưa kịp chạy finally → tự cập nhật trạng thái ngay (tránh render sai)
    t["code"] = code
    t["status"] = "killed"
    t["wait_failed"] = False
    if not fallback_warning:
        t["stop_unconfirmed"] = False
    return render(t, incremental=False) + fallback_warning, bool(fallback_warning)


def resume_starts() -> None:
    """Allow starts when a server lifecycle begins on this event loop."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = False


async def shutdown() -> dict:
    """Best-effort cleanup of this process's registered background tasks."""
    global _SHUTTING_DOWN, _SHUTDOWN_EPOCH
    _SHUTTING_DOWN = True
    _SHUTDOWN_EPOCH += 1
    pending = set(_STARTS)
    done = set()
    if pending:
        done, pending = await asyncio.wait(pending, timeout=SHUTDOWN_START_WAIT)
    tasks = [t for t in list(_TASKS.values()) if t["status"] == "running"]
    results = await asyncio.gather(*(_kill_task(t) for t in tasks), return_exceptions=True)
    failures = sum(isinstance(r, BaseException) or r[1] for r in results) + sum(f.result() for f in done)
    loop = asyncio.get_running_loop()
    pumps = {p for p in _PUMPS if p.get_loop() is loop and not p.done()}
    pending_pumps = set()
    if pumps:
        # Do not cancel pumps on timeout: they may still drain if an inherited
        # pipe closes later. This wait is separate from process termination.
        _, pending_pumps = await asyncio.wait(pumps, timeout=SHUTDOWN_PUMP_WAIT)
    return {"attempted": len(tasks), "pending_starts": len(pending),
            "pending_pumps": len(pending_pumps), "failures": failures}


def summary(workspace: Path, *, owner_id: str = "", session_id: str = "") -> str:
    scope = _scope(workspace, owner_id, session_id)
    tasks = [t for t in _TASKS.values() if t.get("scope") == scope]
    if not tasks:
        return "(chưa có tác vụ nền nào)"
    rows = []
    for t in sorted(tasks, key=lambda x: x["started"]):
        mark = {"running": "⏳", "done": "✅", "failed": "❌", "killed": "🛑"}.get(t["status"], "·")
        if t.get("wait_failed") or t.get("stop_unconfirmed"):
            mark = "⚠"
        warning = " · ⚠ output may be incomplete" if t.get("output_read_failed") else ""
        if t.get("wait_failed"):
            warning += " · ⚠ exit status could not be confirmed"
        if t.get("stop_unconfirmed"):
            warning += " · ⚠ process or group stop could not be confirmed"
        rows.append(f"{mark} [{t['id']}] {t['cmd'][:60]}{warning}")
    return "\n".join(rows)
