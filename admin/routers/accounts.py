"""账号管理：列表 / 新增 / 批量上传 / 扫描本机 / 导入本机 / 刷新余额 / 启用禁用 / 删除 / 注入本机客户端。"""
import glob
import json
import os
import shutil
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend
from admin.config import settings
from admin.db import get_db
from admin.models import Account
from admin.security import require_admin

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    name: Optional[str] = None  # 留空则自动取真实昵称 / uid
    auth_json: str


class AccountBatchIn(BaseModel):
    items: list[AccountIn]


class InjectIn(BaseModel):
    confirm: bool = False


class ImportLocalIn(BaseModel):
    files: list[str] = []  # 指定文件名；含 "all" 或 all=true 表示全部
    all: bool = False


def _client_auth_dir() -> str:
    d = settings.CLIENT_AUTH_DIR or os.path.expandvars(
        r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth"
    )
    return os.path.expandvars(d)


def _apply_meta(acc: Account, auth_json: str):
    meta = backend.parse_auth_meta(auth_json)
    acc.auth_json = auth_json
    if meta.get("uid"):
        acc.uid = meta["uid"]
    if meta.get("enterprise_id"):
        acc.enterprise_id = meta["enterprise_id"]
    if meta.get("domain"):
        acc.domain = meta["domain"]
    # 号池名称：真实昵称 → uid → 兜底（昵称为 null/空串/"null" 视为缺失）
    nick = meta.get("nickname")
    if isinstance(nick, str):
        nick = nick.strip()
    if not acc.name:
        acc.name = (nick if nick and nick.lower() != "null" else None) or meta.get("uid") or "未命名"


def _refresh_balance(acc: Account) -> bool:
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            bal = sess.fetch_balance()
            acc.balance_total = int(bal.get("total", 0) or 0)
            acc.balance_remain = int(bal.get("remain", 0) or 0)
            # 顺带查成长中心连登天数（活跃展示）：复用同一会话零额外开销；
            # 查询失败不影响余额结果（streak_days 保持旧值）
            try:
                acc.streak_days = sess.growth_streak_days()
            except Exception:
                pass
            acc.auth_json = sess.updated_json()  # 回写可能刷新的 token
        acc.last_sync_at = datetime.utcnow()
        acc.err_count = 0  # 余额拉取成功 = 会话存活，清零三振计数
        return True
    except Exception:
        return False


@router.get("")
def list_accounts(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(Account).order_by(Account.id.desc()).all()
    items = [
        {
            "id": a.id,
            "name": a.name,
            "uid": a.uid,
            "enterprise_id": a.enterprise_id,
            "domain": a.domain,
            "status": a.status,
            "balance_total": a.balance_total,
            "balance_remain": a.balance_remain,
            "streak_days": a.streak_days,
            "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            "last_used_at": a.last_used_at.isoformat() if a.last_used_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]
    summary = {
        "total": len(items),
        "active": sum(1 for i in items if i["status"] == "active"),
        "available": sum(1 for i in items if i["status"] == "active" and i["balance_remain"] > 0),
        "balance_total": sum(i["balance_total"] for i in items),
        "balance_remain": sum(i["balance_remain"] for i in items),
    }
    return {"items": items, "summary": summary}


@router.post("")
def add_account(body: AccountIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        json.loads(body.auth_json)
    except Exception:
        raise HTTPException(status_code=400, detail="auth_json 不是合法 JSON")
    acc = Account(name=body.name) if body.name else Account()
    _apply_meta(acc, body.auth_json)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    _refresh_balance(acc)
    db.commit()
    return {"id": acc.id, "name": acc.name, "ok": True}


@router.post("/batch")
def batch_add(body: AccountBatchIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    added = 0
    errors = []
    for it in body.items:
        if not it.auth_json or not it.auth_json.strip():
            continue
        try:
            json.loads(it.auth_json)
        except Exception:
            errors.append("跳过一条：auth_json 非法 JSON")
            continue
        acc = Account(name=it.name) if it.name else Account()
        _apply_meta(acc, it.auth_json)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if _refresh_balance(acc):
            added += 1
        else:
            errors.append(f"账号 {acc.id} 余额刷新失败（凭据可能失效）")
        db.commit()
    return {"added": added, "errors": errors}


@router.get("/scan-local")
def scan_local(_: bool = Depends(require_admin)):
    """扫描本机 WorkBuddy/CodeBuddy 登录态目录，列出发现的账号（不读 token 内容到前端）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        return {"dir": d, "exists": False, "active_uid": None, "items": []}
    active_uid = None
    active_file = os.path.join(d, "workbuddy-desktop.info")
    if os.path.exists(active_file):
        try:
            active_uid = (json.load(open(active_file, encoding="utf-8")).get("account") or {}).get("uid")
        except Exception:
            pass
    items = []
    seen_uids = set()  # 按 uid 去重
    for f in sorted(glob.glob(os.path.join(d, "*.info"))):
        base = os.path.basename(f)
        if base.endswith(".bak") or ".bak-" in base:
            continue
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        meta = backend.parse_auth_meta(json.dumps(data))
        uid = meta.get("uid") or ""
        # 同 uid 只保留一份（优先保留无时间戳后缀的"实时登录"文件）
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        # 提取 token 到期时间
        auth = data.get("auth") or {}
        expires_at = auth.get("expiresAt") or 0
        expires_str = ""
        if expires_at:
            try:
                expires_str = datetime.fromtimestamp(expires_at / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                expires_str = str(expires_at)
        # 提取昵称：过滤字面量 "null" 字符串
        raw_nick = meta.get("nickname") or ""
        nickname = raw_nick if raw_nick.strip().lower() not in ("null", "", "none") else ""
        items.append({
            "file": base,
            "uid": uid,
            "nickname": nickname,
            "domain": meta.get("domain") or "",
            "is_active": (uid == active_uid),
            "expires_at": expires_str,
            "expires_ts": expires_at,
        })
    return {"dir": d, "exists": True, "active_uid": active_uid, "items": items}


@router.post("/import-local")
def import_local(body: ImportLocalIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把本机登录态目录里的 .info 直接读入号池（服务端读取，不向前端暴露 token）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    names = list(body.files)
    if body.all or "all" in names:
        names = [
            os.path.basename(f) for f in glob.glob(os.path.join(d, "*.info"))
            if not (f.endswith(".bak") or ".bak-" in f)
        ]
    added = 0
    errors = []
    seen = {a.uid for a in db.query(Account).all()}  # 已存在的 uid 跳过，避免重复导入
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            errors.append(f"文件不存在: {name}")
            continue
        try:
            auth = open(path, encoding="utf-8").read()
            json.loads(auth)
        except Exception:
            errors.append(f"{name}: 读取/解析失败")
            continue
        meta = backend.parse_auth_meta(auth)
        uid = meta.get("uid")
        if uid and uid in seen:
            continue  # 同 uid 多文件 / 已存在，只导入一次
        seen.add(uid)
        acc = Account()
        _apply_meta(acc, auth)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if _refresh_balance(acc):
            added += 1
        else:
            errors.append(f"账号 {acc.id}({acc.name}) 余额刷新失败（凭据可能失效）")
        db.commit()
    return {"added": added, "errors": errors}


@router.post("/{acc_id}/inject")
def inject_to_client(acc_id: int, body: InjectIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把号池里某账号的 .info 注入到本机客户端的活动登录文件（先备份当前登录态）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true 才执行注入")
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    target = os.path.join(d, "workbuddy-desktop.info")
    backup = None
    if os.path.exists(target):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target + f".bak-{ts}"
        shutil.copy2(target, backup)
    with open(target, "w", encoding="utf-8") as f:
        f.write(acc.auth_json)
    return {"ok": True, "target": target, "backup": backup, "account": acc.name, "uid": acc.uid}


@router.post("/{acc_id}/refresh")
def refresh_account(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    ok = _refresh_balance(acc)
    db.commit()
    if not ok:
        raise HTTPException(status_code=502, detail="刷新失败：后端调用异常（凭据/限流）")
    return {"id": acc.id, "balance_total": acc.balance_total, "balance_remain": acc.balance_remain,
            "streak_days": acc.streak_days}


@router.get("/{acc_id}/credit-details")
def credit_details(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """获取账号积分明细（每个积分包的总量/剩余/到期时间）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            packages = sess.fetch_credit_details()
            # 回写可能刷新的 token
            acc.auth_json = sess.updated_json()
            db.commit()
        return {"id": acc_id, "account": acc.name, "packages": packages}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取积分明细失败: {e}")


@router.get("/{acc_id}/request-usage")
def request_usage(
    acc_id: int,
    start_time: str = "",
    end_time: str = "",
    page_num: int = 1,
    page_size: int = 10,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """获取账号模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    # 默认今天
    if not start_time:
        start_time = datetime.now().strftime("%Y-%m-%d 00:00:00")
    if not end_time:
        end_time = datetime.now().strftime("%Y-%m-%d 23:59:59")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            data = sess.fetch_request_usage(start_time, end_time, page_num, page_size)
            acc.auth_json = sess.updated_json()
            db.commit()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取请求用量失败: {e}")


@router.patch("/{acc_id}")
def patch_account(
    acc_id: int,
    body: dict,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if "name" in body:
        acc.name = body["name"]
    if "status" in body and body["status"] in ("active", "disabled"):
        acc.status = body["status"]
        if body["status"] == "active":
            # 手动重新启用：清空三振计数与冷却，给账号一个干净的重开状态
            acc.err_count = 0
            acc.cool_until = None
            acc.cool_kind = ""
    db.commit()
    return {"id": acc.id, "ok": True}


@router.delete("/{acc_id}")
def delete_account(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.delete(acc)
    db.commit()
    return {"id": acc_id, "ok": True}
