"""OAuth 登录相关接口：在账号页点一下就能加号，不需要桌面端。

路由前缀刻意与被 `prefix="/api/accounts"` 的账号路由分开（`/api/oauth`），
避免 `/api/accounts/{acc_id}/...` 这类动态段与 `/api/accounts/oauth/...` 争抢匹配
（两者段数相同，FastAPI 按注册顺序匹配，`{acc_id}` 为 int 时会先把 "oauth" 当参数解析而报 422）。

三个入口均需管理员登录态：
  POST /api/oauth/start              发起授权，返回 login_id + auth_url
  GET  /api/oauth/status/{login_id}  查询是否已完成（只回账号元信息，不回 token）
  POST /api/oauth/commit/{login_id}  领取凭据并写入号池（一次性）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import oauth_login
from admin.db import get_db
from admin.models import Account
from admin.routers.accounts import _apply_meta, _refresh_balance
from admin.security import require_admin

_LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oauth", tags=["oauth"])


class CommitIn(BaseModel):
    name: str = ""  # 留空则用真实昵称，其次 uid


@router.post("/start")
def start(_: bool = Depends(require_admin)):
    """发起 OAuth 授权。返回的 auth_url 需要人工在浏览器打开完成登录。"""
    try:
        return oauth_login.start_login()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"发起授权失败：{e}")


@router.get("/status/{login_id}")
def status(login_id: str, _: bool = Depends(require_admin)):
    """查询登录进度。pending 表示还没在浏览器里完成登录，可反复轮询。"""
    try:
        return oauth_login.poll_login(login_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期，请重新发起")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"查询登录状态失败：{e}")


@router.post("/commit/{login_id}")
def commit(login_id: str, body: CommitIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """领取凭据并入库。

    同 uid 已存在时执行「更新」而不是报错——这样账号 token 失效后重新授权即可，
    不会在号池里堆出重复条目。
    """
    try:
        cred = oauth_login.take_credentials(login_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="登录会话不存在、已过期或凭据已被领取")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    auth_json: str = cred["auth_json"]
    meta: dict = cred["meta"]
    uid = meta.get("uid") or ""

    existing = None
    if uid:
        existing = db.query(Account).filter(Account.uid == uid).first()

    if existing is not None:
        _apply_meta(existing, auth_json)  # 覆盖 auth_json 并同步 uid/domain
        if body.name:
            existing.name = body.name
        # 重新授权后清掉历史冷却/错误计数，让它立刻回到可用状态
        existing.status = "active"
        existing.err_count = 0
        existing.cool_until = None
        existing.cool_kind = ""
        db.commit()
        ok = _refresh_balance(existing)
        db.commit()
        _LOGGER.info("OAuth 重新授权并更新账号 id=%s uid=%s", existing.id, uid)
        return {
            "ok": True, "updated": True, "id": existing.id, "name": existing.name,
            "uid": uid,
            "balance_remain": existing.balance_remain,
            "balance_refreshed": ok,
        }

    acc = Account(name=body.name or None) if body.name else Account()
    _apply_meta(acc, auth_json)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    ok = _refresh_balance(acc)
    db.commit()
    _LOGGER.info("OAuth 新增账号 id=%s uid=%s", acc.id, uid)
    return {
        "ok": True, "updated": False, "id": acc.id, "name": acc.name,
        "uid": uid,
        "balance_remain": acc.balance_remain,
        "balance_refreshed": ok,
    }


@router.get("/pending")
def pending(_: bool = Depends(require_admin)):
    """当前未完成的登录会话数（排查用，确认没有悬挂的 state）。"""
    return {"pending": oauth_login.pending_count()}
