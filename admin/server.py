"""管理后台 FastAPI 入口：登录、挂载路由、托管前端静态页、启动时建库建表。"""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from admin.config import settings
from admin.db import ensure_database, init_db, SessionLocal
from admin.models import SystemSetting
from admin.routers import accounts, keys, logs, models, oauth, proxy, schedules, sync
from admin.ratelimit import clear_failures, get_client_ip, is_locked, record_failure
from admin.security import (
    create_admin_token,
    hash_password,
    require_admin,
    verify_password,
)

# 可选内嵌独立网关 converter：把它挂到 /gw 前缀下，实现「单端口单进程」部署。
# converter 用本机桌面登录态直连后端，并额外提供 /v1/responses、/v1/messages（Anthropic）、
# /v1/balance 等协议；与管理后台自带的 /v1/chat/completions、/v1/models（带 Key 配额托管）
# 路径互不冲突。缺依赖时自动降级为只跑管理后台。
try:
    from converter import app as converter_app, CONFIG as _conv_cfg
    _CONVERTER_EMBEDDED = True
except Exception:  # pragma: no cover - 降级分支
    converter_app = None
    _CONVERTER_EMBEDDED = False

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="WorkBuddy 共享管理后台", version="1.0")

# CORS：共享网关以 Authorization 头鉴权、不使用 Cookie，故关闭 credentials；
# 避免 `allow_origins=["*"] + allow_credentials=True` 的危险组合。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_headers=["*"],
    allow_methods=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """统一安全响应头：防点击劫持 / MIME 嗅探 / 敏感头泄露。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith(("/api", "/admin")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

app.include_router(accounts.router)
app.include_router(oauth.router)
app.include_router(keys.router)
app.include_router(models.router)
app.include_router(proxy.router)
app.include_router(sync.router)
app.include_router(schedules.router)
app.include_router(logs.router)


@app.on_event("startup")
def _startup():
    ensure_database()
    init_db()
    from admin.scheduler import start_scheduler
    start_scheduler()


def _get_stored_hash() -> str:
    """读取已存储的密码哈希；无记录或旧明文则迁移为哈希后持久化。"""
    try:
        db = SessionLocal()
        row = db.query(SystemSetting).filter(SystemSetting.key == "admin_password").first()
        if row and row.value:
            val = row.value
            if not val.startswith("pbkdf2$"):  # 旧明文 → 迁移为哈希
                val = hash_password(val)
                row.value = val
                db.commit()
            return val
    finally:
        db.close()
    # 无记录：用配置默认密码并持久化哈希
    h = hash_password(settings.ADMIN_PASSWORD)
    try:
        db = SessionLocal()
        db.add(SystemSetting(key="admin_password", value=h))
        db.commit()
    except Exception:
        pass
    finally:
        db.close()
    return h


@app.post("/api/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    request: Request = None,
):
    ip = get_client_ip(
        request.headers.get("X-Forwarded-For") if request else None,
        request.client.host if request and request.client else None,
    )
    locked, wait = is_locked(ip)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )
    if username != settings.ADMIN_USERNAME or not verify_password(password, _get_stored_hash()):
        record_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    # 仅用户名正确且密码正确才清空失败计数（避免可被探测用户名是否存在）
    clear_failures(ip)
    return {"token": create_admin_token()}


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


@app.patch("/api/admin/password")
def change_password(body: PasswordChangeIn, _: bool = Depends(require_admin)):
    if not verify_password(body.old_password, _get_stored_hash()):
        raise HTTPException(status_code=403, detail="当前密码不正确")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于 6 位")
    db = SessionLocal()
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == "admin_password").first()
        if row:
            row.value = hash_password(body.new_password)
        else:
            db.add(SystemSetting(key="admin_password", value=hash_password(body.new_password)))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.get("/")
def index_redirect():
    return RedirectResponse(url="/admin")


@app.get("/admin")
@app.get("/admin/")
def admin_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 内嵌 converter 网关到 /gw 前缀（单端口部署）。设置脱敏与日志路径以对齐独立运行时的行为。
if _CONVERTER_EMBEDDED:
    import os

    _conv_cfg["desensitize"] = os.getenv("CONVERTER_DESENSITIZE", "1") != "0"
    _conv_cfg["log_path"] = os.getenv(
        "CONVERTER_LOG", str(STATIC_DIR.parent.parent / "logs" / "converter-embedded.log")
    )

    # 补上登录凭据。CONFIG["cred"] 只在 converter.py 的 main() 里赋值，而这里是
    # import 进来挂载的（main() 永不执行），所以它默认恒为 None —— 结果是 /gw 下
    # 所有需要凭据的端点一律 503（含 Claude Code 用的 /gw/v1/messages）。
    # 这里按独立运行时的同样逻辑补一次初始化。
    # 注意：服务若以非桌面登录账户运行，需用 CODEBUDDY_AUTH_DIR 指向该账户的 auth 目录。
    if _conv_cfg.get("cred") is None:
        try:
            from converter import CredentialManager, find_auth_file

            _auth_file = find_auth_file()
            if _auth_file:
                _conv_cfg["cred"] = CredentialManager(_auth_file)
        except Exception:
            pass

    # /gw 的鉴权开关。converter._check_auth() 的实现是 `if not key: return` ——
    # key 为空就等于完全不鉴权，而本服务默认监听 0.0.0.0，那等于把桌面端账号的
    # 额度向整个网络开放。所以只认环境变量，不做任何默认值兜底。
    _conv_cfg["api_key"] = os.getenv("CONVERTER_API_KEY", "")

    _log = logging.getLogger("admin.server")
    if _conv_cfg.get("cred") is None:
        _log.warning(
            "/gw 未取得桌面端登录凭据，/gw/v1/* 将返回 503。"
            "请确认桌面端已登录，或用 CODEBUDDY_AUTH_DIR 指定 auth 目录。"
        )
    elif not _conv_cfg["api_key"]:
        _log.warning(
            "/gw 可用但 CONVERTER_API_KEY 为空：/gw/v1/* 不校验任何 Key，"
            "任何能访问本端口的人都能消耗账号额度。建议在 .env 中设置。"
        )

    app.mount("/gw", converter_app)
