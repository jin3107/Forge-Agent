"""Forge Agent — FastAPI server: SSE agent streaming, sessions, workspace files, static web UI.

Run:  uvicorn server.main:app --host 0.0.0.0 --port 8000
"""
# ruff: noqa: B008 — FastAPI Depends trong default là DI idiom của toàn bộ routes.
import asyncio
import hashlib
import io
import json
import logging
import os
import sys
import time
import zipfile
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import background, checkpoints, config, guard, llm, tools
from engine.hook_review_requests import ReviewRequests
from engine.agent import Agent, run_batch, run_battle, send_demo
from engine.hooks import HookTrustDenied
from engine.auth import CSRF_COOKIE, User, csrf_valid
from engine.mcp import load_servers, save_servers
from engine.mcp import manager as mcp_manager
from engine.models import CAPABILITIES, ModelCapabilityError
from engine.models import registry as model_registry
from engine.models import router as model_router
from engine.session import ForkError, ScopedSessionStore, SessionStore
from server import auth as auth_web
from server.auth import require_admin, require_user
from server.upload_limit import UploadRequestLimit

@asynccontextmanager
async def lifespan(app: FastAPI):
    background.resume_starts()
    try:
        yield
    finally:
        # Wait concurrently so a slow spawn or checkpoint cleanup cannot delay
        # signaling the independently tracked background process groups.
        result, foreground_pending, checkpoint_pending = await asyncio.gather(
            background.shutdown(), tools.wait_foreground_start_cleanups(),
            checkpoints.wait_git_cleanups(), return_exceptions=True)
        logger = logging.getLogger(__name__)
        if isinstance(result, BaseException):
            logger.warning("Background shutdown failed: %s", type(result).__name__)
        elif result["pending_starts"] or result.get("pending_pumps", 0) or result["failures"]:
            logger.warning("Background shutdown incomplete: %s", result)
        if isinstance(foreground_pending, BaseException):
            logger.warning("Foreground cleanup wait failed: %s", type(foreground_pending).__name__)
        elif foreground_pending:
            logger.warning("Foreground spawn cleanup incomplete: %s pending", foreground_pending)
        if isinstance(checkpoint_pending, BaseException):
            logger.warning("Checkpoint cleanup wait failed: %s", type(checkpoint_pending).__name__)
        elif checkpoint_pending:
            logger.warning("Checkpoint cleanup incomplete: %s pending", checkpoint_pending)


app = FastAPI(title="Forge Agent", version="0.38.0", lifespan=lifespan)
app.include_router(auth_web.router)
# Security and CORS middleware registered below wrap this limiter.
app.add_middleware(UploadRequestLimit)
# Development giữ CORS mở cho tooling; release mặc định same-origin. Muốn mở
# cross-origin phải liệt kê rõ FORGE_CORS_ORIGINS=https://app.example,...
_cors = [x.strip() for x in os.environ.get("FORGE_CORS_ORIGINS", "").split(",") if x.strip()]
if config.is_release_mode() and "*" in _cors:
    raise RuntimeError("FORGE_CORS_ORIGINS không được chứa '*' ở release mode")
if _cors or not config.is_release_mode():
    app.add_middleware(
        CORSMiddleware, allow_origins=_cors or ["*"], allow_methods=["*"], allow_headers=["*"],
        allow_credentials=bool(_cors),
    )
store = SessionStore()
_hook_review_requests = ReviewRequests()


def _db(user: User) -> ScopedSessionStore:
    return ScopedSessionStore(store, user.id, allow_mcp=user.role == "admin")


def _checked_hook_runner(owner_id: str):
    """Build a per-agent checked runner from the authenticated server scope."""
    from .hook_runner import ScopedCheckedHookRunner
    return ScopedCheckedHookRunner(
        owner_id=owner_id,
        resolve=lambda owner, session, workspace: _hook_scope_snapshot_with_inheritance(
            owner, session, str(workspace)),
        load_config=config.load_config)


STATIC_DIR = ROOT / "server" / "static"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ChatBody(BaseModel):
    text: str
    model: str | None = None
    mode: str = "agent"  # direct | agent | plan | architect
    capability: str = "code"  # code | reasoning | vision | design | tools
    images: list[str] = []  # data URI image/png|jpeg|webp|gif (tối đa 4)
    rerun: bool = False  # true: chạy lại từ tin user đã sửa (qua /rerun) — không append tin mới
    language: str = "vi"  # Web UI language for scripted demo content


class BattleBody(BaseModel):
    prompt: str
    models: list[str] = []
    language: str = "vi"


class ExecBody(BaseModel):
    command: str


class OpenWorkspaceBody(BaseModel):
    path: str


class SettingsBody(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    approval_mode: str | None = None
    docker_sandbox: bool | None = None
    max_tokens: int | None = None
    web_search: bool | None = None
    allow_commands: list[str] | None = None
    deny_commands: list[str] | None = None
    custom_base_url: str | None = None
    custom_api_key: str | None = None
    auto_commit: bool | None = None
    reasoning_effort: str | None = None


class FileSaveBody(BaseModel):
    path: str
    content: str
    # Optional for older API clients; the editor always supplies this token.
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ConditionalFileSaveBody(FileSaveBody):
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class McpServerBody(BaseModel):
    name: str
    command: str
    args: list[str] = []


class BatchBody(BaseModel):
    prompts: list[str]
    model: str | None = None
    language: str = "vi"


class ApprovalBody(BaseModel):
    decision: str  # "approve" | "approve_always" | "deny"


def sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": []}
_LLM_RATE: dict[str, list[float]] = {}


def _rate_retry(identity: str, limit: int,
                bucket: dict[str, list[float]] | None = None) -> int:
    """Sliding window 60s theo user; in-process cho deployment một VPS."""
    now = time.monotonic()
    rates = _LLM_RATE if bucket is None else bucket
    rows = rates.setdefault(identity, [])
    cutoff = now - 60.0
    rows[:] = [t for t in rows if t > cutoff]
    if len(rows) >= limit:
        return max(1, int(60.0 - (now - rows[0])) + 1)
    rows.append(now)
    if len(rates) > 1000:
        for key in list(rates):
            if not rates[key] or rates[key][-1] <= cutoff:
                rates.pop(key, None)
    return 0


def _security_error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", **(headers or {})})


@app.middleware("http")
async def release_security(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        user = auth_web.optional_user(request)
        if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and not csrf_valid(request.headers.get("x-csrf-token"),
                                   request.cookies.get(CSRF_COOKIE),
                                   request.headers.get("sec-fetch-site"))):
            return _security_error(403, "CSRF token không hợp lệ")
        public = path == "/api/health" or path.startswith("/api/auth/")
        if not public and user is None and request.method != "OPTIONS":
            return _security_error(401, "Cần đăng nhập")
        expensive = (
            path in {"/api/battle", "/api/batch"}
            or (path.startswith("/api/sessions/")
                and path.rsplit("/", 1)[-1] in {"chat", "compact", "init", "loop"})
        )
        if user and request.method == "POST" and expensive:
            host = request.client.host if request.client else "unknown"
            retry = _rate_retry(f"{user.id}|{host}", config.get_rate_limit_per_min())
            if retry:
                return _security_error(429, "Quá nhiều request LLM — thử lại sau",
                                       {"Retry-After": str(retry)})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Health / settings
# ---------------------------------------------------------------------------
def _pkg_version() -> str:
    """Đọc version pyproject 1 LẦN lúc import (không I/O trong request handler)."""
    try:
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as source:
            return tomllib.load(source)["project"]["version"]
    except Exception:  # noqa: BLE001 — thiếu version không được làm chết health
        return "?"


APP_VERSION = _pkg_version()


@app.get("/api/health")
async def health():
    """Public liveness: không trả model/key/settings hay dữ liệu user."""
    return {"ok": True, "version": APP_VERSION, "demo": not config.has_any_llm_credentials(),
            "release_mode": config.is_release_mode(), "multi_user": True,
            "auth": "google_or_email"}


async def _settings_payload():
    provider = config.get_provider()
    key = config.get_active_api_key()
    cfg = config.load_config()
    release = config.is_release_mode()
    return {
        "ok": True,
        "version": APP_VERSION,
        "demo": not config.has_any_llm_credentials(),
        "provider": provider,
        "release_mode": release,
        "settings_locked": release,
        "web_key_input": not release,
        "model": model_router.default_model(),
        "key_masked": "••••••••" if key else "",
        "provider_keys": {"openrouter": bool(config.get_api_key()),
                          "google": bool(config.get_google_api_key()),
                          "custom": bool(config.get_custom_api_key())},
        "default_models": [spec.id for spec in model_registry.list()],
        "approval_mode": cfg.get("approval_mode", "guarded"),
        "docker_sandbox": bool(cfg.get("docker_sandbox")),
        "docker_available": guard.DOCKER_AVAILABLE,
        "max_tokens": config.get_max_tokens(),
        "web_search": bool(cfg.get("web_search")),
        "allow_commands": cfg.get("allow_commands") or [],
        "deny_commands": cfg.get("deny_commands") or [],
        "active_profile": cfg.get("active_profile"),
        "profiles": sorted(config.list_profiles().keys()),
        "custom_base_url": config.get_base_url(),
        "auto_commit": config.get_auto_commit(),
        "reasoning_effort": config.get_reasoning_effort(),
        "lsp_available": _lsp_available(),
    }


def _lsp_available() -> dict:
    """Ngôn ngữ nào có language server trên PATH (để UI hiển thị trạng thái)."""
    import shutil
    return {
        "python": shutil.which("pylsp") is not None or shutil.which("pyright-langserver") is not None,
        "typescript": shutil.which("typescript-language-server") is not None,
    }


@app.get("/api/settings")
async def get_settings(user: User = Depends(require_user)):
    payload = await _settings_payload()
    if user.role != "admin":
        payload.update({"settings_locked": True, "web_key_input": False,
                        "key_masked": "", "custom_base_url": ""})
        payload.pop("provider_keys", None)
    return payload


class KeyCheckBody(BaseModel):
    provider: str = "openrouter"
    api_key: str | None = None


@app.post("/api/keycheck")
async def keycheck(body: KeyCheckBody | None = None, user: User = Depends(require_admin)):
    """Verify key theo provider; release khóa endpoint để Settings chỉ qua terminal."""
    if config.is_release_mode():
        raise HTTPException(403, "Release mode: key chỉ được quản lý qua terminal")
    provider = (body.provider if body else "openrouter").strip().lower()
    if provider not in ("openrouter", "google"):
        raise HTTPException(400, "Key check hỗ trợ openrouter hoặc google")
    provided = body.api_key.strip() if (body and body.api_key) else None
    return await llm.check_key(provided, provider=provider)


@app.post("/api/settings")
async def set_settings(body: SettingsBody, user: User = Depends(require_admin)):
    if config.is_release_mode():
        raise HTTPException(403, "Release mode: Settings bị khóa — cấu hình qua scripts/setup_keys.py")
    patch: dict = {}
    if body.api_key is not None and body.api_key.strip():
        # Provider của key độc lập với default routing; không ghi nhầm vào active provider.
        key_provider = (body.provider or config.get_provider()).strip().lower()
        key_fields = {"openrouter": "api_key", "google": "google_api_key",
                      "custom": "custom_api_key"}
        if key_provider not in key_fields:
            raise HTTPException(400, "Provider của API key không hợp lệ")
        patch[key_fields[key_provider]] = body.api_key.strip()
    if body.model is not None and body.model.strip():
        try:
            patch["model"] = model_registry.route("code", body.model).id
        except (ValueError, ModelCapabilityError) as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.approval_mode is not None and body.approval_mode in ("guarded", "ask", "auto"):
        patch["approval_mode"] = body.approval_mode
    if body.docker_sandbox is not None:
        patch["docker_sandbox"] = body.docker_sandbox
    if body.max_tokens is not None and 256 <= int(body.max_tokens) <= 65536:
        patch["max_tokens"] = int(body.max_tokens)
    if body.web_search is not None:
        patch["web_search"] = bool(body.web_search)
    if body.allow_commands is not None:
        patch["allow_commands"] = [x.strip() for x in body.allow_commands if x.strip()][:50]
    if body.deny_commands is not None:
        patch["deny_commands"] = [x.strip() for x in body.deny_commands if x.strip()][:50]
    if body.custom_base_url is not None:  # chuỗi rỗng = quay lại OpenRouter
        patch["custom_base_url"] = body.custom_base_url.strip().rstrip("/")
    if body.custom_api_key is not None and body.custom_api_key.strip():
        patch["custom_api_key"] = body.custom_api_key.strip()
    if body.auto_commit is not None:
        patch["auto_commit"] = bool(body.auto_commit)
    if body.reasoning_effort is not None:  # sai giá trị → về mặc định (không gửi)
        e = body.reasoning_effort.strip().lower()
        patch["reasoning_effort"] = e if e in ("low", "medium", "high") else ""
    config.save_config(patch)
    return await _settings_payload()


# ---------------------------------------------------------------------------
# Connectors OAuth 2.1 (C6) — flow chạy nền, web poll /status
# ---------------------------------------------------------------------------
class OAuthStartBody(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None


@app.get("/api/connectors/presets")
async def connector_presets(user: User = Depends(require_admin)):
    from engine import connectors as fc
    from engine import oauth as fo
    installed = {fc._sanitize_name(c.get("preset", "")) for c in fc.load_servers()}
    out = []
    for pid, p in fc.PRESETS.items():
        out.append({
            "id": pid, "label": p["label"], "auth": p["auth"],
            "auth_hint": p["auth_hint"], "installed": pid in installed,
            "oauth": bool(p.get("oauth")),
            "connected": fo.has_oauth(pid), "masked": fo.masked(pid),
        })
    return {"presets": out}


class InstallBody(BaseModel):
    url: str = ""
    write: bool = False


@app.post("/api/connectors/install")
async def connector_install(body: InstallBody, id: str, user: User = Depends(require_admin)):
    from engine import connectors as fc
    pid = (id or "").strip().lower()
    if pid not in fc.PRESETS:
        raise HTTPException(404, f"Không có preset '{id}'")
    msg = fc.add(pid, body.url, body.write)
    if msg.startswith("❌"):
        raise HTTPException(400, msg)
    name = fc.PRESETS[pid]["label"].split(" — ")[0].split(" (")[0]
    return {"ok": True, "message": msg, "name": name}


@app.delete("/api/connectors/{id}/installed")
async def connector_uninstall(id: str, user: User = Depends(require_admin)):
    from engine import connectors as fc
    pid = (id or "").strip().lower()
    for cfg in fc.load_servers():
        if cfg.get("preset", "") == pid:
            return {"ok": True, "message": fc.remove(cfg["name"])}
    raise HTTPException(404, f"Preset '{id}' chưa cài")


@app.post("/api/connectors/{name}/oauth/start")
async def oauth_start(name: str, body: OAuthStartBody, user: User = Depends(require_admin)):
    from engine import connectors as fc
    from engine import oauth as fo
    pid = (name or "").strip().lower()
    if pid not in fc.PRESETS or not fc.PRESETS[pid].get("oauth"):
        raise HTTPException(404, f"Preset '{name}' không hỗ trợ OAuth")
    if body.client_id:
        fo.save_creds(pid, body.client_id, body.client_secret or "")
    import threading
    threading.Thread(target=fo.connect, args=(pid,), daemon=True).start()
    return {"ok": True, "note": "Đang chờ duyệt trong trình duyệt — hỏi /status cho tới connected/error"}


@app.get("/api/connectors/{name}/oauth/status")
async def oauth_status(name: str, user: User = Depends(require_admin)):
    from engine import connectors as fc
    from engine import oauth as fo
    if (name or "").strip().lower() not in fc.PRESETS:
        raise HTTPException(404, "Unknown preset")
    return fo.flow_status(name.strip().lower())


@app.delete("/api/connectors/{name}/oauth")
async def oauth_disconnect(name: str, user: User = Depends(require_admin)):
    from engine import oauth as fo
    return {"ok": fo.forget(name)}


# ---------------------------------------------------------------------------
# MCP servers management
# ---------------------------------------------------------------------------
@app.get("/api/mcp")
async def mcp_status(user: User = Depends(require_admin)):
    return {"servers": mcp_manager.status()}


@app.post("/api/mcp")
async def mcp_add(body: McpServerBody, user: User = Depends(require_admin)):
    servers = load_servers()
    if any(s["name"] == body.name for s in servers):
        raise HTTPException(400, f"Server '{body.name}' đã tồn tại")
    servers.append({"name": body.name, "command": body.command, "args": body.args, "enabled": True})
    save_servers(servers)
    return {"servers": mcp_manager.status()}


@app.post("/api/mcp/{name}/refresh")
async def mcp_refresh(name: str, user: User = Depends(require_admin)):
    await mcp_manager.refresh(name if name != "all" else None)
    return {"servers": mcp_manager.status()}


@app.delete("/api/mcp/{name}")
async def mcp_remove(name: str, user: User = Depends(require_admin)):
    servers = load_servers()
    kept = [s for s in servers if s["name"] != name]
    if len(kept) == len(servers):
        raise HTTPException(404, f"Không tìm thấy server '{name}'")
    save_servers(kept)
    # stop the process if running
    from engine.mcp import _sanitize_name
    srv = mcp_manager._servers.pop(_sanitize_name(name), None)
    if srv:
        await srv.stop_quiet()
    return {"servers": mcp_manager.status()}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
async def list_sessions(q: str = "", user: User = Depends(require_user)):
    db = _db(user)
    return {"sessions": db.list(q=q)}


class RenameBody(BaseModel):
    title: str


class SessionModelBody(BaseModel):
    model: str


@app.post("/api/sessions/{sid}/rename")
async def rename_session(sid: str, body: RenameBody, user: User = Depends(require_user)):
    db = _db(user)
    if not db.rename(sid, body.title):
        raise HTTPException(404, "Session not found")
    return {"ok": True, "title": body.title.strip()[:80]}


@app.post("/api/sessions")
async def create_session(body: dict | None = None, user: User = Depends(require_user)):
    db = _db(user)
    model = (body or {}).get("model")
    try:
        canonical = model_router.canonical(str(model)) if model else model_router.default_model()
        model_registry.route("code", canonical)
    except (ValueError, ModelCapabilityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    s = db.create(model=canonical)
    return {"session": {"id": s.id, "title": s.title, "model": s.model,
                        "message_count": 0, "updated_at": s.updated_at}}


@app.get("/api/sessions/{sid}")
async def get_session(sid: str, user: User = Depends(require_user)):
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return {"session": s.to_dict()}


@app.patch("/api/sessions/{sid}/model")
async def set_session_model(sid: str, body: SessionModelBody,
                            user: User = Depends(require_user)):
    """Lưu lựa chọn model ngay, không chờ prompt đầu tiên; vẫn qua owner ACL + CSRF."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    requested = body.model.strip()
    if not requested or len(requested) > 300:
        raise HTTPException(400, "Model ID rỗng hoặc dài quá 300 ký tự")
    try:
        canonical = model_router.canonical(requested)
        model_registry.route("code", canonical)
    except (ValueError, ModelCapabilityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    s.model = canonical
    db.save(s)
    return {"ok": True, "model": s.model,
            "session": {"id": s.id, "title": s.title, "model": s.model,
                        "message_count": len(s.messages), "updated_at": s.updated_at}}


@app.get("/api/usage")
async def usage_totals(user: User = Depends(require_user)):
    db = _db(user)
    tot = {"sessions": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "total_tokens": 0, "cost": 0.0}
    for meta in db.list():
        tot["sessions"] += 1
        u = meta.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            tot[k] += u.get(k, 0) or 0
        tot["cost"] += u.get("cost", 0.0) or 0.0
    tot["cost"] = round(tot["cost"], 4)
    return tot


# ---------------------------------------------------------------------------
# Interactive command approvals ("ask" mode)
# ---------------------------------------------------------------------------
@app.post("/api/approvals/{aid}")
async def resolve_approval(aid: str, body: ApprovalBody, user: User = Depends(require_user)):
    from engine import approvals
    ok = approvals.resolve(aid, body.decision, owner_id=user.id)
    if not ok:
        raise HTTPException(404, "Yêu cầu phê duyệt không tồn tại hoặc đã xử lý")
    return {"ok": True, "decision": body.decision}


class ElicitBody(BaseModel):
    action: str = "cancel"          # accept | decline | cancel
    content: dict | None = None


@app.get("/api/elicitations")
async def pending_elicitations(user: User = Depends(require_user)):
    """Danh sách elicitation MCP đang chờ người dùng (poll fallback cho UI)."""
    from engine import elicitations
    return {"pending": elicitations.snapshot(owner_id=user.id)}


@app.post("/api/elicitation/{eid}")
async def resolve_elicitation(eid: str, body: ElicitBody, user: User = Depends(require_user)):
    """Trả lời elicitation MCP (spec: action = accept|decline|cancel + content)."""
    from engine import elicitations
    ok = elicitations.resolve(eid, body.action, body.content, owner_id=user.id)
    if not ok:
        raise HTTPException(404, "Elicitation không tồn tại hoặc đã xử lý")
    return {"ok": True, "action": body.action}


@app.get("/api/models")
async def list_models(user: User = Depends(require_user), provider: str | None = None,
                      capability: str | None = None):
    """Model catalog nhiều provider; client nhận ID/metadata, không nhận endpoint/key."""
    if capability is not None and capability not in CAPABILITIES:
        raise HTTPException(400, "Capability không hợp lệ")
    if capability == "image_generation":
        return {"models": [], "count": 0, "image_generation_adapter": False,
                "detail": "Capability contract đã có; image adapter thuộc phase sau"}
    if provider is not None and provider not in ("openrouter", "google", "custom"):
        raise HTTPException(400, "Provider không hợp lệ")
    if time.time() - _MODELS_CACHE["ts"] > 3600 or not _MODELS_CACHE["data"]:
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.get("https://openrouter.ai/api/v1/models")
                r.raise_for_status()
                items = r.json().get("data", [])
            data = []
            for m in items:
                model_id = str(m.get("id", "")).strip()
                if not model_id:
                    continue
                pr = m.get("pricing") or {}
                try:
                    prompt_p = float(pr.get("prompt") or 0)
                    comp_p = float(pr.get("completion") or 0)
                except (TypeError, ValueError):
                    prompt_p = comp_p = 0.0
                spec = model_registry.get(f"openrouter:{model_id}")
                capabilities = set(spec.capabilities)
                modalities = (m.get("architecture") or {}).get("input_modalities") or []
                if "image" in modalities:
                    capabilities.update({"vision", "design"})
                data.append({"id": spec.id, "provider": "openrouter", "model": model_id,
                             "name": m.get("name", model_id), "label": m.get("name", model_id),
                             "capabilities": sorted(capabilities), "adapter": "chat",
                             "context": m.get("context_length"), "prompt_price": prompt_p,
                             "completion_price": comp_p, "free": prompt_p == 0 and comp_p == 0})
            data.sort(key=lambda x: x["id"])
            _MODELS_CACHE["data"] = data
            _MODELS_CACHE["ts"] = time.time()
        except Exception:
            # Catalog tĩnh vẫn dùng được khi mất mạng; không chặn Google direct.
            if not _MODELS_CACHE["data"]:
                _MODELS_CACHE["ts"] = 0
    rows = list(_MODELS_CACHE["data"])
    known = {row["id"] for row in rows}
    for spec in model_registry.list():
        if spec.id not in known:
            rows.append({**spec.public(), "name": spec.label, "context": None,
                         "prompt_price": 0.0, "completion_price": 0.0, "free": False})
            known.add(spec.id)
    if config.get_provider() == "custom":
        spec = model_registry.get(model_registry.canonical(config.get_model(), "custom"))
        if spec.id not in known:
            rows.append({**spec.public(), "name": spec.label, "context": None,
                         "prompt_price": 0.0, "completion_price": 0.0, "free": True})
    rows = [row for row in rows if (provider is None or row["provider"] == provider)
            and (capability is None or capability in row.get("capabilities", []))]
    rows.sort(key=lambda x: x["id"])
    return {"models": rows, "count": len(rows), "image_generation_adapter": False,
            "capabilities": sorted(CAPABILITIES)}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str, user: User = Depends(require_user)):
    db = _db(user)
    ok = db.delete(sid)
    return {"ok": ok}


# ----------------------------------------------------- S8: Skills catalog (web tab)
@app.get("/api/skills")
async def skills_list(sid: str, user: User = Depends(require_user)):
    db = _db(user)
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    from engine.skills import list_skills
    skills, diags = list_skills(db.workspace(sid))
    rows = []
    for x in skills:
        rows.append({
            "name": x["name"], "description": x.get("description", ""),
            "path": str(x.get("path")), "source": x.get("source", ""),
            "fmt": x.get("fmt", ""), "model_invocable": bool(x.get("model_invocable", True)),
            "trusted": bool(x.get("trusted", True)),
            "allowed_tools": x.get("allowed_tools", ""),
            "diags": x.get("diags") or [],
        })
    return {"skills": rows, "diags": diags[:15]}


class SkillToggleBody(BaseModel):
    path: str
    invocable: bool


@app.post("/api/skills/toggle")
async def skills_toggle(sid: str, body: SkillToggleBody, user: User = Depends(require_user)):
    db = _db(user)
    from engine import skillmgr
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    ws = db.workspace(sid)
    if user.role != "admin":
        from engine.skills import list_skills
        skills, _ = list_skills(ws)
        match = next((x for x in skills if str(x.get("path")) == str(Path(body.path))), None)
        if match is not None and match.get("source") != "project":
            raise HTTPException(403, "Member chỉ đổi skill trong project workspace của mình")
    msg = skillmgr.set_model_invocable(ws, body.path, body.invocable)
    code = 400 if msg.startswith("❌") else 200
    if code == 400:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


class SkillTrustBody(BaseModel):
    name: str


@app.post("/api/skills/trust")
async def skills_trust(sid: str, body: SkillTrustBody, user: User = Depends(require_user)):
    db = _db(user)
    from engine import skillmgr
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    msg = skillmgr.trust(db.workspace(sid), body.name)
    code = 400 if msg.startswith("❌") else 200
    if code == 400:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


# ----------------------------------------------------- F2: cross-session messaging
class MessageBody(BaseModel):
    text: str
    from_session: str = "web"


@app.post("/api/sessions/{sid}/messages")
async def message_send(sid: str, body: MessageBody, user: User = Depends(require_user)):
    db = _db(user)
    from engine import bus as fbus
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    if not body.text.strip():
        raise HTTPException(400, "Empty message")
    from_session = body.from_session or "web"
    if from_session != "web" and db.get(from_session) is None:
        raise HTTPException(404, "Source session not found")
    ent = fbus.send(sid, from_session, body.text)
    return {"ok": True, "entry": ent}


@app.get("/api/sessions/{sid}/messages")
async def message_inbox(sid: str, peek: bool = False, user: User = Depends(require_user)):
    db = _db(user)
    from engine import bus as fbus
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    rows = fbus.peek(sid) if peek else fbus.inbox(sid)
    return {"messages": rows, "count": len(rows)}


# ----------------------------------------------------- B2: worktree sub-agent
@app.get("/api/worktrees")
async def worktree_list(user: User = Depends(require_user)):
    from engine import worktree as fwt
    own = {row["id"] for row in _db(user).list()}
    return {"worktrees": [row for row in await fwt.list_all() if row.get("session") in own]}


@app.post("/api/worktrees/{sub_sid}/merge")
async def worktree_merge(sub_sid: str, sid: str, user: User = Depends(require_user)):
    db = _db(user)
    from engine import worktree as fwt
    st = db.get(sid)
    if not st or db.get(sub_sid) is None:
        raise HTTPException(404, "Session not found")
    return await fwt.merge(sub_sid, sid, sid, db.workspace(sid))


@app.delete("/api/worktrees/{sub_sid}")
async def worktree_discard(sub_sid: str, sid: str, user: User = Depends(require_user)):
    db = _db(user)
    from engine import worktree as fwt
    if not db.get(sid) or db.get(sub_sid) is None:
        raise HTTPException(404, "Session not found")
    return await fwt.discard(sub_sid, sid)


# ----------------------------------------------------- F1: /loop — chạy định kỳ
class LoopBody(BaseModel):
    text: str
    every: str = "30m"  # "45s" | "30m" | "2h" | số = phút
    mode: str = "agent"
    max_runs: int = 0  # 0 = tới khi /loop stop hoặc restart server


async def _loop_turn(sid: str, text: str, mode: str, owner_id: str, allow_mcp: bool) -> str:
    """Mỗi nhịp tự mở scoped store; không giữ quyền/global store trong closure."""
    db = ScopedSessionStore(store, owner_id, allow_mcp=allow_mcp)
    st = db.get(sid)
    if not st:
        return "(phiên đã xoá)"
    demo = not config.has_provider_credentials(llm.split_model_id(st.model)[0])
    if demo:
        agent = Agent(db, sid, router=model_router)
    else:
        # Same scoped checked runner as normal non-demo chat; the demo tick stays outside this policy.
        agent = Agent(db, sid, router=model_router, hook_runner=_checked_hook_runner(owner_id))
    source = send_demo(agent, text) if demo else agent.send(text, chat_mode=mode)
    last = ""
    async for ev in source:
        if ev.get("type") == "assistant_message" and ev.get("content"):
            last = str(ev["content"])
    return last


def _loop_runner(owner_id: str, allow_mcp: bool):
    async def run(sid: str, text: str, mode: str) -> str:
        return await _loop_turn(sid, text, mode, owner_id, allow_mcp)
    return run


@app.post("/api/sessions/{sid}/loop")
async def loop_start(sid: str, body: LoopBody, user: User = Depends(require_user)):
    db = _db(user)
    from engine import scheduler as fsched
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    if not body.text.strip():
        raise HTTPException(400, "Loop cần nội dung tác vụ")
    every_s = fsched.parse_interval(body.every, default_minutes=30)
    mode = body.mode if body.mode in ("direct", "agent", "plan", "architect") else "agent"
    max_runs = max(0, min(int(body.max_runs), 10000))
    status = fsched.manager.start(sid, every_s, body.text.strip(), mode, _loop_runner(user.id, user.role == "admin"), max_runs)
    return {"ok": True, "status": status,
            "note": "Loop sống trong tiến trình server — restart là dừng"}


@app.get("/api/sessions/{sid}/loop")
async def loop_status(sid: str, user: User = Depends(require_user)):
    if not _db(user).get(sid):
        raise HTTPException(404, "Session not found")
    from engine import scheduler as fsched
    return fsched.manager.status(sid)


@app.delete("/api/sessions/{sid}/loop")
async def loop_stop(sid: str, user: User = Depends(require_user)):
    if not _db(user).get(sid):
        raise HTTPException(404, "Session not found")
    from engine import scheduler as fsched
    return {"ok": fsched.manager.stop(sid)}


# ----------------------------------------------------- E5: chỉnh sửa tin & chạy lại
class RerunBody(BaseModel):
    index: int
    text: str


@app.post("/api/sessions/{sid}/rerun")
async def rerun_session(sid: str, body: RerunBody, user: User = Depends(require_user)):
    """Cắt session từ tin user thứ `index` và thay nội dung mới — web sẽ gọi /chat rerun=true."""
    db = _db(user)
    st = db.get(sid)
    if not st:
        raise HTTPException(404, "Session not found")
    if not (0 <= body.index < len(st.messages)):
        raise HTTPException(400, f"index {body.index} ngoài phạm vi (0..{len(st.messages) - 1})")
    if st.messages[body.index].get("role") != "user":
        raise HTTPException(400, "index không trỏ vào tin nhắn của người dùng")
    st.messages = st.messages[:body.index] + [{"role": "user", "content": body.text}]
    db.save(st)
    return {"ok": True, "messages": len(st.messages)}


# ----------------------------------------------------- comfort: undo/compact/init
@app.post("/api/sessions/{sid}/undo")
async def undo_session(sid: str, user: User = Depends(require_user)):
    """↩ Hoàn tác thay đổi gần nhất của agent trong workspace (checkpoint git nội bộ)."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    from engine import checkpoints
    if not checkpoints.available():
        raise HTTPException(409, "git không có mặt trên máy — checkpoint không khả dụng")
    ws = db.workspace(sid)
    res = await checkpoints.undo(checkpoints.key_for(ws), ws)
    return {"ok": bool(res.get("ok")), "message": str(res.get("message", ""))}


@app.post("/api/sessions/{sid}/compact")
async def compact_session_ep(sid: str, body: dict | None = None, user: User = Depends(require_user)):
    """🧹 Nén hội thoại: LLM tóm tắt các tin nhắn cũ, giữ vài tin cuối."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    from engine.agent import compact_session
    agent = Agent(db, sid, router=model_router, hook_runner=_checked_hook_runner(user.id))
    try:
        res = await compact_session(agent, (body or {}).get("instructions") or "")
    except HookTrustDenied as exc:
        return {"ok": False, "code": "hook_trust_denied", "message": str(exc)}
    return res


@app.post("/api/sessions/{sid}/init")
async def init_agents_ep(sid: str, user: User = Depends(require_user)):
    """🚀 /init — phân tích workspace, sinh AGENTS.md (demo-mode khi chưa có key)."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    from engine.agent import init_agents_md
    agent = Agent(db, sid, router=model_router)
    res = await init_agents_md(agent)
    return res


@app.post("/api/sessions/{sid}/fork")
async def fork_session(sid: str, user: User = Depends(require_user)):
    """🍴 Fork — nhân bản phiên: copy messages + workspace thành bản mới."""
    db = _db(user)
    try:
        ns = db.fork(sid)
    except ForkError as exc:
        raise HTTPException(409, str(exc)) from None
    if ns is None:
        raise HTTPException(404, "Session not found")
    return {"session": {"id": ns.id, "title": ns.title, "model": ns.model,
                        "message_count": len(ns.messages),
                        "updated_at": ns.updated_at}}


@app.get("/api/sessions/{sid}/context")
async def session_context(sid: str, user: User = Depends(require_user)):
    """🧠 Mức dùng context của phiên (cho /context + footer web)."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return Agent(db, sid).context_stats()


def _audit_rows(user: User, n: int) -> list[dict]:
    from engine import auditlog
    own = {row["id"] for row in _db(user).list()}
    scan = min(10000, max(200, n * 20))
    return [e for e in auditlog.tail(scan) if e.get("session") in own][:n]


@app.get("/api/audit")
async def audit_tail(n: int = 120, user: User = Depends(require_user)):
    """Nhật ký tool call (JSONL data/audit/) — n giới hạn 1..500."""
    return {"entries": _audit_rows(user, max(1, min(n, 500)))}


@app.get("/api/audit/export")
async def audit_export(user: User = Depends(require_user)):
    """Xuất audit log CSV (tối đa 2000 mục mới nhất) — Excel/Sheets đọc trực tiếp."""
    import csv as _csv
    import io as _io

    from fastapi.responses import Response

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["ts", "session", "tool", "source", "ok", "ms", "args", "note"])
    for e in _audit_rows(user, 2000):
        w.writerow([e.get("ts", ""), e.get("session", ""), e.get("tool", ""),
                    e.get("source", ""), "1" if e.get("ok") else "0",
                    "" if e.get("ms") is None else e.get("ms"),
                    json.dumps(e.get("args") or {}, ensure_ascii=False),
                    e.get("note", "")])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="forge-audit.csv"'})


@app.get("/api/doctor")
async def doctor_endpoint(user: User = Depends(require_admin)):
    """🩺 Chẩn đoán toàn hệ thống (python/git/key/model/MCP/LSP/dữ liệu…)."""
    db = _db(user)
    from engine.diagnostics import doctor
    return await doctor(db)


@app.get("/api/sessions/{sid}/export")
async def export_session(sid: str, user: User = Depends(require_user)):
    """⤓ /export — tải hội thoại dạng Markdown."""
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    from urllib.parse import quote

    from engine.export import to_markdown
    md = to_markdown(s)
    fname = f"forge-{s.id}.md"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


# ---------------------------------------------------------------------------
# Chat streaming (SSE)
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, body: ChatBody, request: Request, user: User = Depends(require_user)):
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    text = body.text.strip()
    if not text and not body.rerun:
        raise HTTPException(400, "Empty message")

    model = body.model or None
    chat_mode = body.mode if body.mode in ("direct", "agent", "plan", "architect") else "agent"
    ws = db.workspace(sid)

    # /run <tên> <task> — custom agent từ .forge/agents/<name>.md (đè model/mode nếu có)
    from engine import agents as fagents
    agent_exp = fagents.expand(text, ws)
    if agent_exp is not None:
        text, ag_model, ag_mode = agent_exp
        if ag_model:
            model = ag_model
        if ag_mode:
            chat_mode = ag_mode
    demo = not config.has_provider_credentials(llm.split_model_id(model or s.model)[0])
    # /audit [n] — xem nhật ký tool call, không cần LLM
    audit_synthetic: list[dict] | None = None
    if text == "/audit" or text.startswith("/audit "):
        from engine import auditlog
        a_arg = text[6:].strip()
        a_n = min(200, max(1, int(a_arg))) if a_arg.isdigit() else 20
        audit_synthetic = [{"type": "assistant_message",
                            "content": auditlog.format_listing(a_n, rows=_audit_rows(user, a_n))}]
    # /msg <phiên> <nội dung> + /inbox — F2 cross-session messaging
    msg_synthetic: list[dict] | None = None
    if text.startswith("/msg ") or text == "/inbox":
        from engine import bus as fbus
        if text == "/inbox":
            rows = fbus.inbox(sid)
            if rows:
                m_out = "\n\n".join(f"📥 **{r['from']}** ({str(r['ts'])[11:16]}): {r['text'][:300]}"
                                    for r in rows)
            else:
                m_out = "(hộp thư trống)"
            msg_synthetic = [{"type": "assistant_message", "content": m_out}]
        else:
            parts = text[5:].strip().split(None, 1)
            to_sid = parts[0].strip() if parts else ""
            m_text = parts[1].strip() if len(parts) > 1 else ""
            if not to_sid or not m_text:
                msg_synthetic = [{"type": "assistant_message",
                                  "content": "❌ Cú pháp: `/msg <id-phiên> <nội dung>`"}]
            elif db.get(to_sid) is None:
                msg_synthetic = [{"type": "assistant_message",
                                  "content": f"❌ Không có phiên '{to_sid}'."}]
            else:
                fbus.send(to_sid, sid, m_text)
                msg_synthetic = [{"type": "assistant_message",
                                  "content": f"📨 Đã gửi tới {to_sid} — phiên đích sẽ thấy ở lượt kế tiếp."}]
    # /worktree [list|merge <sub>|discard <sub>] — B2 quản lý worktree sub-agent
    wt_synthetic: list[dict] | None = None
    if text == "/worktree" or text.startswith("/worktree "):
        from engine import worktree as fwt
        w_arg = text[9:].strip()
        if w_arg.startswith("merge "):
            sub = w_arg[6:].strip()
            if db.get(sub) is None:
                raise HTTPException(404, "Session not found")
            res = await fwt.merge(sub, sid, sid, ws)
            wt_synthetic = [{"type": "assistant_message",
                             "content": ("✅ " if res["ok"] else "❌ ") + res["message"]}]
        elif w_arg.startswith("discard "):
            sub = w_arg[8:].strip()
            if db.get(sub) is None:
                raise HTTPException(404, "Session not found")
            res = await fwt.discard(sub, sid)
            wt_synthetic = [{"type": "assistant_message",
                             "content": ("🗑 Đã bỏ worktree: " + ", ".join(res["removed"]))
                                        if res["ok"] else ("❌ " + res.get("message", "Không thể bỏ worktree"))}]
        else:
            own = {row["id"] for row in db.list()}
            rows = [row for row in await fwt.list_all() if row.get("session") in own]
            if not rows:
                wt_synthetic = [{"type": "assistant_message",
                                 "content": "Không có worktree nào. Dùng: spawn_subagent với isolation=worktree, rồi `/worktree merge <phiên-con>` để gộp."}]
            else:
                lines = ["### 📁 Worktree đang cô lập"]
                for r in rows:
                    lines.append(f"- `{r['session']}` — {r['stat']}\n  `{r['path']}`")
                lines.append("Gộp: `/worktree merge <phiên-con>` · Bỏ: `/worktree discard <phiên-con>`")
                wt_synthetic = [{"type": "assistant_message", "content": "\n".join(lines)}]
    # /loop [interval] [off|status] — lịch chạy định kỳ trong tiến trình (F1)
    loop_synthetic: list[dict] | None = None
    if text == "/loop" or text.startswith("/loop "):
        from engine import scheduler as fsched
        l_arg = text[5:].strip()
        if l_arg.lower() in ("off", "stop", "x"):
            was = fsched.manager.stop(sid)
            loop_synthetic = [{"type": "assistant_message",
                               "content": "⚪ Đã dừng loop." if was else "(không có loop nào đang chạy)"}]
        elif not l_arg:
            st_loop = fsched.manager.status(sid)
            if st_loop.get("last_error_code") == "hook_trust_denied":
                loop_message = ("⛔ Loop dừng: hook_trust_denied. " + st_loop["last_error"])
            elif st_loop["running"]:
                loop_message = ("🔁 Đang chạy: mỗi `" + fsched.fmt_interval(st_loop["every"])
                                + "` — «" + st_loop["text"] + "» ("
                                + str(st_loop["runs"]) + " lượt)")
            else:
                loop_message = "Không có loop nào. Cú pháp: `/loop <30m|1h> <tác vụ>` · `/loop off`"
            loop_synthetic = [{"type": "assistant_message", "content": loop_message}]
        else:
            parts = l_arg.split(None, 1)
            every_s = fsched.parse_interval(parts[0], default_minutes=30)
            task_text = parts[1].strip() if len(parts) > 1 else ""
            if not task_text:
                loop_synthetic = [{"type": "assistant_message",
                                   "content": "❌ Thiếu tác vụ: `/loop <30m|1h> <tác vụ>`"}]
            else:
                mode_l = chat_mode if chat_mode != "plan" else "agent"
                status_l = fsched.manager.start(sid, every_s, task_text, mode_l, _loop_runner(user.id, user.role == "admin"))
                loop_synthetic = [{"type": "assistant_message",
                                   "content": f"🔁 Loop BẬT — mỗi `{fsched.fmt_interval(status_l['every'])}` chạy «{task_text}» "
                                              "(tool + audit như chat thường; sống trong tiến trình server, restart là dừng; `/loop off` để tắt)"}]
    # /connectors [add|auth|allow|deny|remove] — quản lý connector MCP, không cần LLM
    connectors_synthetic: list[dict] | None = None
    if text == "/connectors" or text.startswith("/connectors "):
        if user.role != "admin":
            raise HTTPException(403, "Chỉ admin được quản lý connector toàn server")
        from engine import connectors as fconn
        c_arg = text[11:].strip()
        if c_arg.startswith("add "):
            parts = c_arg[4:].strip().split()
            c_out = fconn.add(parts[0], parts[1] if len(parts) > 1 else "", "--write" in parts)
        elif c_arg.startswith("auth "):
            parts = c_arg[5:].strip().split(None, 1)
            c_out = fconn.auth(parts[0], parts[1] if len(parts) > 1 else "")
        elif c_arg.startswith("allow "):
            parts = c_arg[6:].strip().split()
            c_out = fconn.allow(parts[0], parts[1] if len(parts) > 1 else "*")
        elif c_arg.startswith("deny "):
            parts = c_arg[5:].strip().split()
            c_out = fconn.deny(parts[0], parts[1] if len(parts) > 1 else "*")
        elif c_arg.startswith("remove "):
            c_out = fconn.remove(c_arg[7:].strip())
        else:
            c_out = fconn.status_listing()
        connectors_synthetic = [{"type": "assistant_message", "content": c_out}]
    # /skills [add|new|remove …] — quản lý skill (chuẩn Agent Skills), không cần LLM
    skills_synthetic: list[dict] | None = None
    if text == "/skills" or text.startswith("/skills "):
        from engine import skillmgr
        s_arg = text[7:].strip()
        if user.role != "admin" and s_arg.startswith(("add ", "remove ")):
            raise HTTPException(403, "Skill global chỉ admin được cài/xoá; member dùng /skills new trong workspace")
        if s_arg.startswith("add "):
            # git clone có thể tới 90s — đẩy sang thread để không khoá event loop
            s_out = await asyncio.to_thread(skillmgr.add, ws, s_arg[4:].strip())
        elif s_arg.startswith("new "):
            parts = s_arg[4:].strip().split(None, 1)
            s_out = skillmgr.new_skill(ws, parts[0] if parts else "",
                                       parts[1] if len(parts) > 1 else "")
        elif s_arg.startswith("remove "):
            s_out = skillmgr.remove(ws, s_arg[7:].strip())
        elif s_arg.startswith("trust"):
            s_out = skillmgr.trust(ws, s_arg[5:].strip())
        else:
            s_out = skillmgr.format_listing(ws)
        skills_synthetic = [{"type": "assistant_message", "content": s_out}]
    else:
        # /<tên-lệnh> từ .forge/commands & skills chuẩn SKILL.md — expand trước mọi nhánh khác
        from engine import commands as fcmd
        blocked = fcmd.blocked_reason(ws, text)
        if blocked is not None:
            skills_synthetic = [{"type": "assistant_message", "content": blocked}]
        else:
            expanded = fcmd.expand(text, ws)
            if expanded is not None:
                text = expanded
    # ---- lệnh nội bộ không cần LLM: /review /diff /memory và '# ' quick-memory ----
    synthetic: list[dict] | None = (audit_synthetic if audit_synthetic is not None else
                                    (msg_synthetic if msg_synthetic is not None else
                                    (wt_synthetic if wt_synthetic is not None else
                                     (loop_synthetic if loop_synthetic is not None else
                                      (connectors_synthetic
                                       if connectors_synthetic is not None else skills_synthetic)))))
    from engine import memory as fmem
    if text.startswith("/review"):
        from engine import review as freview
        r_arg = text[len("/review"):].strip()
        try:
            diff_text, label = await freview.collect_diff(ws, sid, r_arg)
            if demo:
                synthetic = [{"type": "assistant_message",
                              "content": "(demo mode) /review cần API key để chạy LLM review — "
                                         f"diff đã sẵn sàng ({label}, {len(diff_text):,} ký tự)."}]
            else:
                text = freview.build_prompt(diff_text, r_arg, label)
                chat_mode = "plan"  # review chỉ đọc
        except ValueError as e:
            synthetic = [{"type": "assistant_message", "content": f"🔍 {e}"}]
    elif text.startswith("/diff"):
        from engine import review as freview
        d_arg = text[len("/diff"):].strip()
        try:
            diff_text, label = await freview.collect_diff(ws, sid, d_arg)
            diff_body = diff_text if len(diff_text) <= 12000 else diff_text[:12000] + "\n… (cắt ở 12KB)"
            synthetic = [{"type": "assistant_message",
                          "content": f"📄 **Diff — {label}:**\n\n```diff\n{diff_body}\n```"}]
        except ValueError as e:
            synthetic = [{"type": "assistant_message", "content": f"📄 {e}"}]
    elif text == "/memory" or text.startswith("/memory "):
        m_arg = text[7:].strip()
        synthetic = [{"type": "assistant_message",
                      "content": fmem.clear_memory(ws) if m_arg == "clear"
                      else fmem.memory_overview(ws)}]
    elif text.startswith("# ") and len(text.strip()) > 2:
        synthetic = [{"type": "assistant_message",
                      "content": fmem.quick_memory(ws, text.strip()[2:].strip())}]

    # @file references → đính kèm nội dung file vào message
    from engine import atfiles
    text, _attached = atfiles.expand_at_refs(text, ws)

    # ảnh đính kèm (multimodal) — content parts kiểu OpenAI/OpenRouter
    content = None
    images = [u for u in (body.images or []) if isinstance(u, str) and u.startswith("data:image/")][:4]
    if images and not demo:
        content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": u}} for u in images]
    capability = "vision" if images and not demo else body.capability
    if capability == "image_generation":
        raise HTTPException(501, "Image generation mới có contract; adapter thuộc phase sau")
    try:
        model = model_registry.route(capability, model or s.model).id
    except (ValueError, ModelCapabilityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    demo = not config.has_provider_credentials(llm.split_model_id(model)[0])

    async def gen():
        try:
            if synthetic is not None:
                for ev in synthetic:
                    if await request.is_disconnected():
                        return
                    yield sse(ev)
                # Client dùng done để phân biệt stream hoàn tất với kết nối bị cắt.
                yield sse({"type": "done"})
                return
            if demo:
                agent = Agent(db, sid, model=model, router=model_router)
            else:
                # Partial enforcement: normal non-demo chat, manual compact, non-demo loop
                # ticks and batch/battle items; demo mode, synthetic requests and other
                # server entry points remain outside this policy.
                agent = Agent(db, sid, model=model, router=model_router,
                              hook_runner=_checked_hook_runner(user.id))
            if demo:
                demo_text = text
                if images:
                    demo_text += (
                        f"\n\n(demo mode: skipped {len(images)} attached image(s) — add an API key to use vision)"
                        if body.language.lower().startswith("en") else
                        f"\n\n(demo mode: bỏ qua {len(images)} ảnh đính kèm — nhập API key để dùng vision)"
                    )
                source = send_demo(agent, demo_text, language=body.language)
            else:
                source = agent.send(text, chat_mode=chat_mode, content=content,
                                    append_user=not body.rerun)
            last_assistant = ""
            async for ev in source:
                if await request.is_disconnected():
                    return
                if ev.get("type") == "assistant_message" and ev.get("content"):
                    last_assistant = str(ev["content"])
                yield sse(ev)
            # B1 auto-commit (opt-in) — sau lượt agent thật, chỉ mode đổi file
            if (not demo and chat_mode in ("agent", "architect")
                    and config.get_auto_commit()):
                try:
                    from engine import autocommit as fac
                    res = await fac.commit_turn(ws, sid, text, last_assistant)
                    if res:
                        yield sse({"type": "auto_commit", "subject": res["subject"],
                                   "files": res["files"]})
                except Exception as _ace:  # noqa: BLE001 — autocommit lỗi chỉ log, không chết lượt
                    print(f"[autocommit] bỏ qua: {_ace}")
        except HookTrustDenied as exc:
            # An auto-compaction trust denial stops the turn and skips auto-commit.
            yield sse({"type": "error", "code": "hook_trust_denied", "message": str(exc)})
        except Exception as e:  # noqa: BLE001 — lỗi lượt phải report SSE, không chết stream
            yield sse({"type": "error", "message": f"Lỗi server: {e}"})
        yield sse({"type": "done"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Batch: parallel sub-agents
# ---------------------------------------------------------------------------
@app.post("/api/batch")
async def batch(body: BatchBody, request: Request, user: User = Depends(require_user)):
    db = _db(user)
    prompts = [p.strip() for p in body.prompts if p.strip()][:5]
    if not prompts:
        raise HTTPException(400, "Không có tác vụ nào")
    try:
        batch_model = model_registry.route("code", body.model).id if body.model else model_router.default_model()
    except (ValueError, ModelCapabilityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    demo = not config.has_provider_credentials(llm.split_model_id(batch_model)[0])

    async def gen():
        try:
            async with aclosing(run_batch(db, [{"prompt": p, "model": batch_model} for p in prompts],
                                          demo=demo, router=model_router, language=body.language,
                                          hook_runner_factory=lambda: _checked_hook_runner(user.id))) as source:
                async for ev in source:
                    if await request.is_disconnected():
                        return
                    yield sse(ev)
        except Exception as e:  # noqa: BLE001 — lỗi batch phải report SSE
            yield sse({"type": "error", "message": f"Lỗi batch: {e}"})
        yield sse({"type": "done"})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.post("/api/battle")
async def battle(body: BattleBody, request: Request, user: User = Depends(require_user)):
    """⚔ Cùng 1 prompt chạy trên nhiều model song song."""
    db = _db(user)
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Thiếu prompt")
    try:
        battle_models = [model_registry.route("code", m).id for m in body.models]
    except (ValueError, ModelCapabilityError) as exc:
        raise HTTPException(400, str(exc)) from exc

    async def gen():
        try:
            async with aclosing(run_battle(db, prompt, battle_models, demo=None, router=model_router,
                                           language=body.language,
                                           hook_runner_factory=lambda: _checked_hook_runner(user.id))) as source:
                async for ev in source:
                    if await request.is_disconnected():
                        return
                    yield sse(ev)
        except Exception as e:  # noqa: BLE001 — lỗi battle phải report SSE
            yield sse({"type": "error", "message": f"Lỗi battle: {e}"})
        yield sse({"type": "done"})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.post("/api/workspace/exec")
async def workspace_exec(sid: str, body: ExecBody, user: User = Depends(require_user)):
    """Terminal tích hợp: NGƯỜI DÙNG chạy lệnh trực tiếp trong workspace."""
    ws = _ws(sid, user)
    cmd = body.command.strip()
    if not cmd:
        raise HTTPException(400, "Thiếu lệnh")
    verdict = guard.assess_command(cmd)
    if verdict["level"] == "danger" and config.load_config().get("approval_mode", "guarded") != "auto":
        return {"blocked": True, "ok": False, "exit_code": None, "timed_out": False,
                "truncated": False, "level": "danger", "reasons": verdict["reasons"],
                "output": f"BLOCKED: lệnh nguy hiểm ({'; '.join(verdict['reasons'])}). "
                          "Đổi sang Auto trong ⚙ nếu bạn thực sự muốn chạy."}
    # A user typing directly into the terminal is the explicit action for ask mode;
    # guarded-mode danger was checked above and Docker policy still applies.
    terminal_override = config.load_config().get("approval_mode", "guarded") == "ask"
    result = await tools.execute_tool("run_command", {"command": cmd}, ws, override=terminal_override)
    return {key: result[key] for key in ("blocked", "ok", "output", "exit_code", "timed_out", "truncated", "checkpoint_status")
            if key in result}


@app.post("/api/workspace/open")
async def workspace_open(sid: str, body: OpenWorkspaceBody, user: User = Depends(require_admin)):
    """Mở một thư mục dự án thật làm workspace. Yêu cầu env FORGE_ALLOW_OPEN=1."""
    db = _db(user)
    import os
    if os.environ.get("FORGE_ALLOW_OPEN", "").strip() != "1":
        raise HTTPException(403,
                            "Tính năng mở dự án thật đang TẮT vì lý do an toàn. "
                            "Chạy server với biến môi trường FORGE_ALLOW_OPEN=1 để bật.")
    _ws(sid, user)  # ensure session exists
    try:
        target = db.open_workspace(sid, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "workspace": str(target)}


@app.post("/api/workspace/close")
async def workspace_close(sid: str, user: User = Depends(require_user)):
    db = _db(user)
    if not db.get(sid):
        raise HTTPException(404, "Session not found")
    db.close_workspace(sid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Workspace files
# ---------------------------------------------------------------------------
def _ws(sid: str, user: User) -> Path:
    db = _db(user)
    s = db.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return db.workspace(sid)


def _safe_path(ws: Path, rel: str) -> Path:
    p = (ws / rel).resolve()
    if p != ws.resolve() and ws.resolve() not in p.parents:
        raise HTTPException(400, "Path escapes workspace")
    return p


@app.get("/api/workspace/hook-review")
async def workspace_hook_review(sid: str, user: User = Depends(require_user)):
    """Read-only preview; this endpoint does not approve or enforce hooks."""
    from engine import hook_inspection
    ws = _ws(sid, user)
    try:
        ws = ws.resolve(strict=True)
        if not ws.is_dir():
            raise OSError("workspace is not a directory")
    except (OSError, RuntimeError):
        raise HTTPException(409, "Workspace identity could not be resolved") from None
    result = hook_inspection.inspect(ws)
    cfg = result.config
    hooks = cfg.get("hooks") if cfg is not None else None
    legacy = cfg.get("on_file_change") if cfg is not None else None
    return JSONResponse({
        "workspace": str(ws),
        "state": result.state,
        "digest": result.digest,
        "hooks": hooks,
        "on_file_change": legacy,
        "overrides_global_hooks": bool(hooks) if cfg is not None else None,
        "enforcement_active": False,
        "enforcement_scope": "normal_non_demo_chat_and_manual_compact",
    }, headers={"Cache-Control": "no-store"})


class HookReviewRequestBody(BaseModel):
    sid: str = Field(min_length=1, max_length=256)
    expected_workspace: str = Field(min_length=1, max_length=4096)
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    share_commands: StrictBool


@app.post("/api/workspace/hook-review-requests")
async def request_hook_review(body: HookReviewRequestBody, user: User = Depends(require_user)):
    """Explicitly share review metadata and hook commands with admins; not approval."""
    from engine import hook_inspection
    if body.share_commands is not True:
        raise HTTPException(422, "Explicit consent to share hook commands is required")
    db = _db(user)
    session = db.get(body.sid)
    if not session or session.id != body.sid:
        raise HTTPException(404, "Session not found")
    try:
        ws = db.workspace(session.id).resolve(strict=True)
        if not ws.is_dir() or str(ws) != body.expected_workspace:
            raise ValueError("workspace changed")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(409, "Workspace changed or could not be resolved; review again") from None
    result = hook_inspection.inspect(ws)
    if result.state != 'hooks' or result.digest != body.expected_digest:
        raise HTTPException(409, "Hook configuration changed or cannot be reviewed; review again")
    row = _hook_review_requests.submit(user.id, session.id, str(ws), result.digest, share_commands=True)
    if row is None:
        raise HTTPException(429, "Hook review request capacity reached; try again later")
    return JSONResponse({"request_id": row.id, "enforcement_active": False, "enforcement_scope": "normal_non_demo_chat_and_manual_compact"}, headers={"Cache-Control":"no-store"})


@app.get("/api/workspace/hook-review-requests")
async def list_hook_review_requests(user: User = Depends(require_user)):
    """Admins see explicitly shared metadata; other users see only their own."""
    rows = _hook_review_requests.list()
    if user.role != 'admin':
        rows = [r for r in rows if r.owner_id == user.id]
    return JSONResponse({"requests": [{"request_id":r.id, "owner_id":r.owner_id,
                                      "session_id":r.session_id, "workspace":r.workspace,
                                      "digest":r.digest, "status":r.status, "snapshot_revalidated":False} for r in rows],
                         "enforcement_active":False, "enforcement_scope":"normal_non_demo_chat_and_manual_compact"}, headers={"Cache-Control":"no-store"})


def _current_requested_workspace(row) -> Path | None:
    """Revalidate the requesting owner, session and workspace without broadening ACLs."""
    try:
        owner = auth_web.auth_store.get(row.owner_id)
        if not owner or not owner.enabled:
            return None
        scoped = ScopedSessionStore(store, row.owner_id, allow_mcp=False)
        session = scoped.get(row.session_id)
        if not session or session.id != row.session_id:
            return None
        ws = scoped.workspace(row.session_id).resolve(strict=True)
        return ws if ws.is_dir() and str(ws) == row.workspace else None
    except Exception:
        # A failed recheck never authorizes disclosure; do not leak details.
        return None


def _hook_scope_snapshot(owner_id: str, session_id: str, workspace: str):
    """Resolve review scope; caller must authenticate owner/session execution scope."""
    from types import SimpleNamespace
    from engine import hook_inspection
    if (not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 256 or
            not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256 or
            not isinstance(workspace, str) or not workspace or len(workspace) > 4096):
        return None
    scope = SimpleNamespace(owner_id=owner_id, session_id=session_id, workspace=workspace)
    def inspect_scope():
        ws = _current_requested_workspace(scope)
        if ws is None or str(ws) != workspace:
            return hook_inspection.Inspection('invalid')
        result = hook_inspection.inspect(ws)
        if _current_requested_workspace(scope) != ws:
            return hook_inspection.Inspection('invalid')
        return result
    def validate_admin(row):
        admin = auth_web.auth_store.get(row.decided_by)
        if not admin or not admin.enabled or admin.role != 'admin':
            return False
        current = _current_requested_workspace(scope)
        if current is None or str(current) != workspace:
            return False
        admin = auth_web.auth_store.get(row.decided_by)
        return bool(admin and admin.enabled and admin.role == 'admin')
    return _hook_review_requests.scope_snapshot(owner_id, session_id, workspace, inspect_scope, validate_admin)


def _hook_scope_snapshot_with_inheritance(owner_id: str, session_id: str, workspace: str):
    """Resolve scoped approval for the checked non-demo server paths; other paths remain ungated.

    The active parent comes from trusted internal lineage tracking, not a client
    claim. Explicit child review records take precedence, including denial.
    """
    from types import SimpleNamespace
    from engine import hook_inheritance, hook_inspection, hook_lineage
    try:
        def child_has_review():
            return any(row.owner_id == owner_id and row.session_id == session_id
                       for row in _hook_review_requests.list())
        had_review = child_has_review()
        direct = _hook_scope_snapshot(owner_id, session_id, workspace)
        if direct is not None:
            return direct
        if had_review or child_has_review():
            return None
        ws = Path(workspace)
        parent = hook_lineage.parent_for_shared_child(owner_id, session_id, ws)
        if parent is None:
            return None
        def relation(owner, parent_id, child, path):
            return (not child_has_review() and
                    hook_lineage.is_shared_child(owner, parent_id, child, path))
        def inspect_child(owner, child, path):
            scope = SimpleNamespace(owner_id=owner, session_id=child, workspace=str(path))
            current = _current_requested_workspace(scope)
            return hook_inspection.inspect(current) if current is not None else None
        return hook_inheritance.inherit_shared_snapshot(
            owner_id=owner_id, parent_sid=parent, child_sid=session_id, workspace=ws,
            resolve_parent=lambda owner, sid, path: _hook_scope_snapshot(owner, sid, str(path)),
            validate_shared_relation=relation, inspect_child_scope=inspect_child)
    except Exception:
        return None


def _approved_hook_snapshot(owner_id: str, session_id: str, workspace: str):
    """Prototype for a future gate; caller must authenticate the execution scope.

    Not wired to hook execution. Revocation, account and filesystem changes
    after this function returns can make its result stale.
    """
    from engine import hook_inspection
    def validate(row):
        def admin_enabled():
            admin = auth_web.auth_store.get(row.decided_by)
            return bool(admin and admin.enabled and admin.role == 'admin')
        if not admin_enabled():
            return None
        ws = _current_requested_workspace(row)
        if ws is None:
            return None
        result = hook_inspection.inspect(ws)
        if result.state != 'hooks' or result.raw is None or result.digest != row.digest:
            return None
        if _current_requested_workspace(row) != ws or not admin_enabled():
            return None
        return result.raw
    return _hook_review_requests.approved_snapshot(owner_id, session_id, workspace, validate)


@app.get("/api/workspace/hook-review-requests/{request_id}")
async def admin_hook_review_request(request_id: str, user: User = Depends(require_admin)):
    """Review explicitly shared hook config after revalidating its scope and digest."""
    from engine import hook_inspection
    row = _hook_review_requests.get(request_id)
    if row is None or not row.share_commands:
        raise HTTPException(404, "Review request not found")
    ws = _current_requested_workspace(row)
    if ws is None:
        _hook_review_requests.invalidate(row)
        raise HTTPException(409, "Review request is stale or unavailable; submit a new request")
    result = hook_inspection.inspect(ws)
    if result.state != 'hooks' or result.digest != row.digest or result.config is None:
        _hook_review_requests.invalidate(row)
        raise HTTPException(409, "Review request is stale or unavailable; submit a new request")
    if _hook_review_requests.get(request_id) != row:
        raise HTTPException(409, "Review request changed or is unavailable")
    if _current_requested_workspace(row) != ws:
        _hook_review_requests.invalidate(row)
        raise HTTPException(409, "Review request is stale or unavailable; submit a new request")
    # The scope recheck itself may overlap a revoke, expiry or decision.
    if _hook_review_requests.get(request_id) != row:
        raise HTTPException(409, "Review request changed or is unavailable")
    cfg = result.config
    return JSONResponse({"request_id":row.id, "owner_id":row.owner_id, "session_id":row.session_id,
                         "workspace":row.workspace, "digest":row.digest, "status":row.status,
                         "hooks":cfg.get('hooks'), "on_file_change":cfg.get('on_file_change'),
                         "overrides_global_hooks":bool(cfg.get('hooks')),
                         "enforcement_active":False, "enforcement_scope":"normal_non_demo_chat_and_manual_compact"}, headers={"Cache-Control":"no-store"})


class HookReviewDecisionBody(BaseModel):
    decision: Literal['approve', 'deny']
    expected_workspace: str = Field(min_length=1, max_length=4096)
    expected_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


@app.post("/api/workspace/hook-review-requests/{request_id}/decision")
async def decide_hook_review_request(request_id: str, body: HookReviewDecisionBody,
                                     user: User = Depends(require_admin)):
    """Record an admin decision; enforcement covers only some execution paths."""
    from engine import hook_inspection
    def validate(row) -> bool:
        ws = _current_requested_workspace(row)
        if ws is None:
            return False
        result = hook_inspection.inspect(ws)
        admin = auth_web.auth_store.get(user.id)
        return bool(result.state == 'hooks' and result.digest == row.digest
                    and _current_requested_workspace(row) == ws
                    and admin and admin.enabled and admin.role == 'admin')
    status = _hook_review_requests.decide(request_id, user.id,
        'approved' if body.decision == 'approve' else 'denied',
        body.expected_workspace, body.expected_digest, validate)
    if status == 'missing':
        raise HTTPException(404, "Review request not found")
    if status not in ('approved', 'denied'):
        raise HTTPException(409, "Review request changed, expired or is no longer actionable")
    return JSONResponse({"request_id":request_id, "status":status, "enforcement_active":False, "enforcement_scope":"normal_non_demo_chat_and_manual_compact"},
                        headers={"Cache-Control":"no-store"})


@app.delete("/api/workspace/hook-review-requests/{request_id}")
async def revoke_hook_review_request(request_id: str, user: User = Depends(require_user)):
    if not _hook_review_requests.revoke(request_id, user.id, user.role == 'admin'):
        raise HTTPException(404, "Review request not found")
    return JSONResponse({"ok":True, "enforcement_active":False, "enforcement_scope":"normal_non_demo_chat_and_manual_compact"}, headers={"Cache-Control":"no-store"})


@app.get("/api/workspace/tree")
async def workspace_tree(sid: str, user: User = Depends(require_user)):
    ws = _ws(sid, user)
    root = ws.resolve()

    def contained(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.resolve().is_relative_to(root)
        except (OSError, RuntimeError):
            return False

    def node(path: Path, depth: int) -> list[dict]:
        items: list[dict] = []
        try:
            entries = sorted((p for p in path.iterdir() if contained(p)),
                             key=lambda p: (p.is_file(), p.name.lower()))
        except Exception:  # noqa: BLE001 — tree lỗi trả rỗng, không chết request
            return []
        for e in entries:
            if e.name in tools.SKIP_DIRS or e.name.startswith("."):
                continue
            if e.is_dir():
                items.append({"name": e.name, "type": "dir",
                              "children": node(e, depth + 1) if depth < 6 else []})
            else:
                items.append({"name": e.name, "type": "file",
                              "size": e.stat().st_size, "path": str(e.relative_to(ws))})
        return items

    return {"tree": node(ws, 0)}


@app.get("/api/workspace/file")
async def read_workspace_file(sid: str, path: str, user: User = Depends(require_user)):
    ws = _ws(sid, user)
    p = _safe_path(ws, path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    # Reject rather than return truncated content that could later be saved over
    # the original. The extra byte detects files that exceed the preview limit.
    with p.open("rb") as f:
        raw = f.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(413, "File too large to preview (limit: 4 MiB)")
    # Never expose a lossy UTF-8 replacement decode as editable text.
    # The binary flag also covers text in other encodings and NUL-containing
    # content; it is a conservative preview policy, not a file-type detector.
    if b"\x00" in raw:
        return {"path": path, "binary": True, "content": "", "version": hashlib.sha256(raw).hexdigest()}
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return {"path": path, "binary": True, "content": "", "version": hashlib.sha256(raw).hexdigest()}
    return {"path": path, "binary": False, "content": text,
            "version": hashlib.sha256(raw).hexdigest()}


def _require_file_version(path: Path, expected: str, action: str = "saved") -> None:
    # Bound this re-read to the same maximum size as the editor preview. A
    # missing, unreadable or enlarged file is a conflict, not permission to save.
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        with path.open("rb") as f:
            data = f.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise HTTPException(409, f"File changed or cannot be verified; file was not {action}") from exc
    if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise HTTPException(409, f"File changed since it was opened; file was not {action}")


@app.put("/api/workspace/file")
async def save_workspace_file(sid: str, body: FileSaveBody, user: User = Depends(require_user)):
    ws = _ws(sid, user)
    p = _safe_path(ws, body.path)
    if p == ws.resolve() or p.is_dir():
        raise HTTPException(400, "Path must be a file")
    encoded = None
    if body.expected_version is not None:
        if "\x00" in body.content:
            raise HTTPException(422, "NUL character in editor content; file was not saved")
        try:
            encoded = body.content.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise HTTPException(422, "Content is not valid UTF-8; file was not saved") from exc
        if len(encoded) > 4 * 1024 * 1024:
            raise HTTPException(413, "File too large to save in editor (limit: 4 MiB UTF-8)")
        _require_file_version(p, body.expected_version)
    outcome = {"status": "failed"}
    try:
        await checkpoints.snapshot(checkpoints.key_for(ws), ws,
                                   f"workspace save: {body.path[:60]}", outcome=outcome)
    except Exception as exc:
        logging.getLogger(__name__).warning("Workspace save checkpoint failed (%s)", type(exc).__name__)
        raise HTTPException(500, "Checkpoint error; file was not saved") from exc
    # The await above allows workspace selection or path symlinks to change.
    # Re-check both; filesystem operations are still not atomic against races.
    if _ws(sid, user).resolve() != ws.resolve():
        raise HTTPException(409, "Workspace changed; file was not saved")
    checked = _safe_path(ws, body.path)
    if checked != p or checked.is_dir():
        raise HTTPException(409, "File path changed; file was not saved")
    if body.expected_version is not None:
        _require_file_version(checked, body.expected_version)
    checked.parent.mkdir(parents=True, exist_ok=True)
    checked.write_text(body.content, encoding="utf-8", newline="")
    result = {"ok": True, "path": body.path, "size": len(body.content),
              "checkpoint_status": outcome["status"]}
    if encoded is not None:
        result["version"] = hashlib.sha256(encoded).hexdigest()
    if outcome["status"] != "created":
        result["warning"] = ("No new checkpoint was confirmed; "
                             "undo may not restore the state before this save.")
    else:
        result["notice"] = ("Checkpoint created; undo may not restore files excluded "
                            "from the snapshot or ignored by Git.")
    return result


@app.put("/api/workspace/file/conditional")
async def save_workspace_file_conditional(sid: str, body: ConditionalFileSaveBody,
                                          user: User = Depends(require_user)):
    # Old servers must reject this route, not silently ignore a version field.
    return await save_workspace_file(sid, body, user=user)


@app.post("/api/workspace/file")
async def create_workspace_file(sid: str, body: FileSaveBody, user: User = Depends(require_user)):
    ws = _ws(sid, user)
    p = _safe_path(ws, body.path)
    if p == ws.resolve() or p.is_dir():
        raise HTTPException(400, "Path must be a file")
    if (ws / body.path).is_symlink() or p.exists():
        raise HTTPException(409, "File already exists; it was not overwritten")
    outcome = {"status": "failed"}
    try:
        await checkpoints.snapshot(checkpoints.key_for(ws), ws,
                                   f"workspace create: {body.path[:60]}", outcome=outcome)
    except Exception as exc:
        logging.getLogger(__name__).warning("Workspace create checkpoint failed (%s)", type(exc).__name__)
        raise HTTPException(500, "Checkpoint error; file was not created") from exc
    if _ws(sid, user).resolve() != ws.resolve():
        raise HTTPException(409, "Workspace changed; file was not created")
    checked = _safe_path(ws, body.path)
    if checked != p or (ws / body.path).is_symlink() or checked.is_dir():
        raise HTTPException(409, "File path changed; file was not created")
    checked.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation prevents overwriting a file created after checks.
        with checked.open("x", encoding="utf-8", newline="") as f:
            f.write(body.content)
    except FileExistsError as exc:
        raise HTTPException(409, "File already exists; it was not overwritten") from exc
    result = {"ok": True, "path": body.path, "size": len(body.content),
              "checkpoint_status": outcome["status"]}
    if outcome["status"] != "created":
        result["warning"] = ("No new checkpoint was confirmed; "
                             "undo may not restore the state before this creation.")
    else:
        result["notice"] = ("Checkpoint created; undo may not restore files excluded "
                            "from the snapshot or ignored by Git.")
    return result


@app.delete("/api/workspace/file")
async def delete_workspace_file(sid: str, path: str, user: User = Depends(require_user),
                                expected_version: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None):
    ws = _ws(sid, user)
    p = _safe_path(ws, path)
    if p == ws.resolve() or p.is_dir():
        raise HTTPException(400, "Path must be a file")
    if expected_version is not None:
        _require_file_version(p, expected_version, action="deleted")
    elif not p.exists():
        raise HTTPException(404, "File not found")
    outcome = {"status": "failed"}
    try:
        await checkpoints.snapshot(checkpoints.key_for(ws), ws,
                                   f"workspace delete: {path[:60]}", outcome=outcome)
    except Exception as exc:
        logging.getLogger(__name__).warning("Workspace delete checkpoint failed (%s)", type(exc).__name__)
        raise HTTPException(500, "Checkpoint error; file was not deleted") from exc
    # Recheck after awaiting, though the check and unlink are not atomic.
    if _ws(sid, user).resolve() != ws.resolve():
        raise HTTPException(409, "Workspace changed; file was not deleted")
    checked = _safe_path(ws, path)
    if checked != p or not checked.exists() or checked.is_dir():
        raise HTTPException(409, "File path changed; file was not deleted")
    if expected_version is not None:
        _require_file_version(checked, expected_version, action="deleted")
    checked.unlink()
    result = {"ok": True, "checkpoint_status": outcome["status"]}
    if outcome["status"] != "created":
        result["warning"] = ("No new checkpoint was confirmed; "
                             "undo may not restore the state before this deletion.")
    else:
        result["notice"] = ("Checkpoint created; undo may not restore files excluded "
                            "from the snapshot or ignored by Git.")
    return result


@app.delete("/api/workspace/file/conditional")
async def delete_workspace_file_conditional(
    sid: str, path: str,
    expected_version: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    user: User = Depends(require_user),
):
    # A distinct endpoint makes old servers fail closed rather than silently
    # ignoring the new version query argument on their legacy DELETE route.
    return await delete_workspace_file(sid, path, user=user, expected_version=expected_version)


@app.post("/api/workspace/upload")
async def upload_files(sid: str, files: list[UploadFile] = File(...), user: User = Depends(require_user),
                       overwrite: Annotated[bool, Form()] = False):
    ws = _ws(sid, user)
    saved, skipped = [], []
    outcome = None
    MAX_SIZE = 20 * 1024 * 1024
    for f in files[:20]:
        name = Path(f.filename or "").name  # strip any path components
        if not name or name.startswith("."):
            skipped.append(f.filename or "?")
            continue
        raw_path = ws / name
        if not overwrite and (raw_path.is_symlink() or raw_path.exists()):
            skipped.append(f"{name} (already exists)")
            continue
        # Read only one byte beyond the limit to detect oversize files. The
        # multipart parser has already received/spooled the request at this point.
        data = await f.read(MAX_SIZE + 1)
        if len(data) > MAX_SIZE:
            skipped.append(f"{name} (>20MB)")
            continue
        if not overwrite and (raw_path.is_symlink() or raw_path.exists()):
            skipped.append(f"{name} (already exists)")
            continue
        p = _safe_path(ws, name)
        if p == ws.resolve() or p.is_dir():
            raise HTTPException(400, "Path must be a file")
        if outcome is None:
            outcome = {"status": "failed"}
            try:
                await checkpoints.snapshot(checkpoints.key_for(ws), ws,
                                           "workspace upload", outcome=outcome)
            except Exception as exc:
                logging.getLogger(__name__).warning("Workspace upload checkpoint failed (%s)", type(exc).__name__)
                raise HTTPException(500, "Checkpoint error; upload was not started") from exc
        # Both reading and checkpointing can yield; recheck on every write.
        # These checks are not atomic with the write.
        if _ws(sid, user).resolve() != ws.resolve():
            raise HTTPException(409, "Workspace changed; upload may be partial")
        if not overwrite and raw_path.is_symlink():
            skipped.append(f"{name} (already exists)")
            continue
        checked = _safe_path(ws, name)
        if checked != p or checked.is_dir():
            raise HTTPException(409, "File path changed; upload may be partial")
        if overwrite:
            checked.write_bytes(data)
        else:
            try:
                with checked.open("xb") as dest:
                    dest.write(data)
            except FileExistsError:
                skipped.append(f"{name} (already exists)")
                continue
        saved.append(name)
    # Report every unprocessed part without reading it. Limit reflected names so
    # unusually long multipart filenames do not inflate this new response field.
    for index, f in enumerate(files[20:], start=21):
        name = Path(f.filename or "").name or "?"
        display = name[:200] + ("…" if len(name) > 200 else "")
        skipped.append(f"#{index}: {display} (over 20-file limit)")
    result = {"saved": saved, "skipped": skipped}
    if outcome is not None:
        result["checkpoint_status"] = outcome["status"]
        if outcome["status"] != "created":
            result["warning"] = ("No new checkpoint was confirmed; "
                                 "undo may not restore the state before this upload.")
        else:
            result["notice"] = ("Checkpoint created; undo may not restore files excluded "
                                "from the snapshot or ignored by Git.")
    return result


@app.get("/api/workspace/export")
async def export_workspace(sid: str, user: User = Depends(require_user)):
    ws = _ws(sid, user)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in tools._iter_files(ws):
            z.write(f, f.relative_to(ws))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=forge-{sid}.zip"},
    )


# ---------------------------------------------------------------------------
# Static web UI (built from web/ → server/static)
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover - dev fallback
    @app.get("/")
    async def root():
        return JSONResponse({"info": "Web UI chưa build. Chạy: cd web && npm install && npm run build"})
