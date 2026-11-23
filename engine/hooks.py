"""Hooks — lifecycle scripts kiểu Claude Code.

Config (config.json hoặc .forge/config.json của workspace):
  "hooks": {
    "pre_tool_use":  [{"command": "...", "matcher": "write_file|edit_file"}],
    "post_tool_use": [{"command": "...", "matcher": "*"}],
    "session_start": [{"command": "..."}],
    "on_file_change": "black ."          # dạng cũ v0.10 — vẫn hoạt động
  }

Hợp đồng (theo Claude Code):
- Hook nhận JSON trên stdin: {session_id, hook_event_name, tool_name, tool_input, workspace}
- exit 0  → tiếp tục
- exit 2  → CHẶN (chỉ pre_tool_use) — stderr là lý do đưa cho LLM
- exit khác → lỗi không chặn, stderr bỏ qua
- post_tool_use: stderr/stdout được nối vào kết quả tool cho LLM thấy
- session_start: stdout được nối vào system prompt (context bổ sung)
- matcher: regex tên tool ("write_file|edit_file", hoặc bỏ/rỗng = khớp tất cả)
"""
import asyncio
import hashlib
import os
import signal

from . import proctree
from .secrets import sanitized_env
import json
import re
from pathlib import Path

from . import config

TIMEOUT = 30.0
# Retain bounded prefixes while continuously draining both subprocess pipes.
HOOK_CAPTURE_BYTES = 64 * 1024
_HOOK_READ_BYTES = 8192
_HOOK_REAP_TIMEOUT = 1.0
_HOOK_TRUNCATED = ' [output truncated]'
# Enabled only by CLI/TUI launchers; server execution is NOT gated by this.
_LOCAL_HOOK_POLICY = False


def enable_local_hook_policy():
    """Fail closed for workspace hooks in this local process; no server linking yet."""
    global _LOCAL_HOOK_POLICY
    _LOCAL_HOOK_POLICY = True


class HookTrustDenied(Exception):
    """Reserved signal for a future trust gate; does not itself enforce policy."""

    def __init__(self):
        super().__init__("Workspace hook trust could not be confirmed; request admin review. "
                         "Earlier operations may have run; check workspace state before retrying.")


class LocalHookTrustDenied(HookTrustDenied):
    """Local sessions cannot yet authenticate a server-admin approval."""

    def __init__(self):
        Exception.__init__(self, "Local workspace hooks are blocked: server-admin review is required, "
                           "but local session linking is not available yet. "
                           "Earlier operations may have run; check workspace state before retrying.")


def _hook_cfgs(workspace: Path, event: str) -> list[dict]:
    if _LOCAL_HOOK_POLICY:
        # Local session identity cannot establish server-admin approval. Select
        # only from the inspected no-hook snapshot; do not reread workspace JSON.
        from . import hook_inspection
        from .hook_approvals import Decision, Snapshot
        from .hook_snapshot import select_entries
        try:
            result = hook_inspection.inspect(workspace)
            if result.state not in ('absent', 'no_hooks'):
                raise LocalHookTrustDenied()
            return select_entries(Decision(result.state, result.digest),
                                  Snapshot(result.state, result.digest, result.raw),
                                  event, config.load_config())
        except HookTrustDenied:
            raise
        except Exception:
            raise LocalHookTrustDenied() from None
    ws_cfg = Path(workspace) / ".forge" / "config.json"
    cfg: dict = {}
    try:
        if ws_cfg.is_file():
            cfg = json.loads(ws_cfg.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — config lỗi → không có hook, không chết tool
        cfg = {}
    hooks = cfg.get("hooks") or config.load_config().get("hooks") or {}
    items = hooks.get(event) or []
    if isinstance(items, str):
        items = [{"command": items}]
    out = []
    for it in items:
        if isinstance(it, str):
            it = {"command": it}
        if isinstance(it, dict) and it.get("command"):
            out.append(it)
    # tương thích v0.10: on_file_change dạng chuỗi
    if event == "post_tool_use":
        legacy = (cfg.get("on_file_change") or config.load_config().get("on_file_change"))
        if legacy:
            out.append({"command": legacy, "matcher": "write_file|edit_file", "_legacy_env": True})
    return out


async def run_event(workspace: Path, sid: str, event: str,
                    extra: dict | None = None) -> list[dict]:
    """C1 (v0.32): chạy mọi hook của event TỔNG QUÁT (user_prompt_submit/stop/
    pre_compact/notification). Trả [{command, rc, out, err}] — CALLER quyết nghĩa
    exit 2 (user_prompt_submit/pre_compact = chặn; stop/notification = bỏ qua rc)."""
    results = []
    payload = {"session_id": sid, "hook_event_name": event,
               "workspace": str(workspace), **(extra or {})}
    for entry in _hook_cfgs(workspace, event):
        res = await _run_cmd(str(entry.get("command")), payload, workspace)
        results.append({"command": str(entry.get("command")),
                        "rc": res.get("code", 1),
                        "out": res.get("out", ""), "err": res.get("err", "")})
    return results


def _matched(entry: dict, tool_name: str) -> bool:
    matcher = str(entry.get("matcher", "") or "").strip()
    if not matcher or matcher == "*":
        return True
    try:
        return re.search(matcher, tool_name or "") is not None
    except re.error:
        return tool_name == matcher


async def _hook_read_prefix(stream):
    captured = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_HOOK_READ_BYTES)
        if not chunk:
            break
        room = max(0, HOOK_CAPTURE_BYTES - len(captured))
        captured.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    return bytes(captured), truncated


async def _hook_capture_merged(proc):
    output = await _hook_read_prefix(proc.stdout)
    await proc.wait()
    return output


async def _hook_write_input(stream, data: bytes):
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
    try:
        await stream.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _hook_communicate_bounded(proc, data: bytes):
    tasks = [asyncio.create_task(_hook_write_input(proc.stdin, data)),
             asyncio.create_task(_hook_read_prefix(proc.stdout)),
             asyncio.create_task(_hook_read_prefix(proc.stderr))]
    try:
        _, out, err = await asyncio.gather(*tasks)
        await proc.wait()
        return out, err
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _hook_format_output(capture, limit: int) -> str:
    raw, truncated = capture
    text = raw.decode('utf-8', 'replace').strip()
    if truncated or len(text) > limit:
        return text[:limit - len(_HOOK_TRUNCATED)] + _HOOK_TRUNCATED
    return text


async def _stop_hook_process(proc):
    """Best effort; an exited leader, escaped child, or interrupted cleanup can survive."""
    if proc.returncode is None or os.name != 'posix':
        # On Windows the wrapper may have exited while the command still runs, so the job
        # is terminated whatever the wrapper's state.
        if os.name == 'posix':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            proctree.terminate_windows_tree(proc)
    if proc.returncode is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_HOOK_REAP_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        pass


async def _run_cmd(command: str, payload: dict, workspace: Path, env_extra: dict | None = None):
    env = sanitized_env({**(env_extra or {}), "FORGE_WORKSPACE": str(workspace)})
    try:
        proc = await proctree.spawn_shell(
            command, cwd=str(workspace), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(
                _hook_communicate_bounded(proc, json.dumps(payload, ensure_ascii=False).encode()), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            await _stop_hook_process(proc)
            return {"code": 1, "out": "", "err": f"hook timeout {TIMEOUT}s; process cleanup unconfirmed"}
        except asyncio.CancelledError:
            await _stop_hook_process(proc)
            raise
        except Exception:
            await _stop_hook_process(proc)
            raise
        return {"code": proc.returncode or 0,
                "out": _hook_format_output(out, 1200),
                "err": _hook_format_output(err, 600)}
    except Exception as e:  # noqa: BLE001 — hook lỗi không được làm hỏng tool
        return {"code": 1, "out": "", "err": f"hook error: {e}"}


async def pre_tool_use(workspace: Path, sid: str, tool_name: str, tool_input: dict) -> dict | None:
    """Trả {"blocked": True, "reason": ...} nếu bị chặn, ngược lại None."""
    for entry in _hook_cfgs(workspace, "pre_tool_use"):
        if not _matched(entry, tool_name):
            continue
        payload = {"session_id": sid, "hook_event_name": "PreToolUse",
                   "tool_name": tool_name, "tool_input": tool_input,
                   "workspace": str(workspace)}
        r = await _run_cmd(entry["command"], payload, workspace)
        if r["code"] == 2:
            return {"blocked": True,
                    "reason": r["err"] or r["out"] or "bị chặn bởi hook pre_tool_use (exit 2)"}
    return None


async def post_tool_use(workspace: Path, sid: str, tool_name: str, tool_input: dict,
                        tool_output: str, env_extra: dict | None = None) -> str:
    """Trả chuỗi bổ sung nối vào tool output (có thể rỗng)."""
    extras: list[str] = []
    for entry in _hook_cfgs(workspace, "post_tool_use"):
        if not _matched(entry, tool_name):
            continue
        if entry.get("_legacy_env"):
            continue  # on_file_change đã chạy trong tools.execute_tool
        payload = {"session_id": sid, "hook_event_name": "PostToolUse",
                   "tool_name": tool_name, "tool_input": tool_input,
                   "tool_output": tool_output[:4000], "workspace": str(workspace)}
        r = await _run_cmd(entry["command"], payload, workspace, env_extra)
        msg = r["err"] or r["out"]
        if msg:
            extras.append(f"🔗 Hook PostToolUse (exit {r['code']}): {msg}")
    return "\n".join(extras)


_SS_CACHE: dict[str, tuple[str, str, str]] = {}
_SS_PENDING: dict[str, object] = {}


async def _session_start_entries(workspace: Path, sid: str, entries: list[dict]) -> str:
    parts: list[str] = []
    for entry in entries:
        r = await _run_cmd(entry["command"], {
            "session_id": sid, "hook_event_name": "SessionStart", "workspace": str(workspace),
        }, workspace)
        if r["out"]:
            parts.append(r["out"])
    return "\n".join(parts)[:2000]


async def session_start_cache(workspace: Path, sid: str) -> str:
    """Cache by session, resolved workspace and selected hook configuration.

    This is not a trust or approval check. Different concurrent cache misses may
    execute hooks independently; only the latest-started result is cached.
    """
    path = str(Path(workspace).resolve())
    # Freeze the selected entries so the key and executed commands correspond.
    serialized = json.dumps(_hook_cfgs(workspace, "session_start"), ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    cached = _SS_CACHE.get(sid)
    if cached and cached[:2] == (path, fingerprint):
        return cached[2]
    token = object()
    _SS_PENDING[sid] = token
    try:
        out = await _session_start_entries(workspace, sid, json.loads(serialized))
        if _SS_PENDING.get(sid) is token:
            _SS_CACHE[sid] = (path, fingerprint, out)
        return out
    finally:
        if _SS_PENDING.get(sid) is token:
            _SS_PENDING.pop(sid, None)


async def session_start(workspace: Path, sid: str) -> str:
    """Run session-start hooks once without caching."""
    return await _session_start_entries(workspace, sid, _hook_cfgs(workspace, "session_start"))
