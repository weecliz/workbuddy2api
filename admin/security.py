"""安全：管理后台 JWT 登录 + API Key 校验与配额判断。"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta

import jwt
from fastapi import Header, HTTPException

from admin.config import settings
from admin.db import SessionLocal
from admin.models import ApiKey


# ---------------------------------------------------------------------------
# 管理后台登录
# ---------------------------------------------------------------------------

def create_admin_token() -> str:
    exp = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": settings.ADMIN_USERNAME, "exp": exp},
                      settings.JWT_SECRET, algorithm="HS256")


def verify_admin_token(token: str | None):
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        if payload.get("sub") != settings.ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="无效令牌")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效令牌")


def require_admin(x_admin_token: str = Header(default=None, alias="X-Admin-Token")):
    """FastAPI 依赖：校验管理后台令牌。"""
    verify_admin_token(x_admin_token)
    return True


# ---------------------------------------------------------------------------
# 管理后台密码（PBKDF2 哈希存储，杜绝明文泄漏）
# ---------------------------------------------------------------------------

_PBKDF2_ITER = 100_000


def hash_password(pw: str) -> str:
    """返回可持久化的哈希串：pbkdf2$<iter>$<salt_b64>$<dk_b64>。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$" + str(_PBKDF2_ITER) + "$" + \
        base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    """校验密码；兼容旧明文存储（首次读取时由 server 统一迁移为哈希）。"""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        return hmac.compare_digest(pw, stored)  # 旧明文兜底
    try:
        _, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(cand, expected)
    except Exception:
        return False


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_key_row(db, api_key: str) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.key_hash == hash_key(api_key)).first()


def check_quota(key: ApiKey) -> None:
    """超额则直接拒绝，提示『积分已耗尽』。

    credit_limit / credit_used 的列定义允许 NULL（旧数据、手工插入），
    而 `None >= None` 会抛 TypeError，把该 Key 的所有 /v1 请求变成 500。
    这里统一把 NULL 视为 0：与 ORM 建行时的 default=0 语义一致。
    """
    if key.status != "active":
        raise HTTPException(status_code=403, detail={"error": {"message": "API Key 已停用", "type": "key_disabled"}})
    if not key.unlimited and (key.credit_used or 0) >= (key.credit_limit or 0):
        raise HTTPException(
            status_code=402,
            detail={"error": {"message": "积分已耗尽", "type": "quota_exceeded"}},
        )
