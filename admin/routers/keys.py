"""API Key 管理：创建（带积分限额）/ 列表 / 查看 / 调整 / 吊销 / 用量。"""
import base64
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.models import ApiKey
from admin.security import hash_key, require_admin

router = APIRouter(prefix="/api/keys", tags=["keys"])


class KeyIn(BaseModel):
    name: str = ""
    credit_limit: float = 0
    unlimited: bool = False
    note: str = ""


def _gen_key() -> str:
    # sk- 前缀 + 256-bit 密码学安全随机（secrets，无时间戳/序号/可推断规律）
    return "sk-" + secrets.token_hex(32)


def _mask(key: str) -> str:
    return key[:10] + "****" + key[-4:] if len(key) > 16 else key[:6] + "****"


def _encode_key(raw: str) -> str:
    """简单编码存储完整 key（base64，非加密；管理后台已有 JWT 保护）。"""
    return base64.b64encode(raw.encode()).decode()


def _decode_key(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode()).decode()
    except Exception:
        return ""


@router.get("")
def list_keys(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    items = [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "masked": _mask((k.key_prefix or "") + "****************"),
            "credit_limit": k.credit_limit,
            "credit_used": k.credit_used,
            "unlimited": bool(k.unlimited),
            "status": k.status,
            "note": k.note,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        }
        for k in rows
    ]
    return {"items": items}


@router.post("")
def create_key(body: KeyIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    raw = _gen_key()
    k = ApiKey(
        name=body.name or "未命名",
        key_hash=hash_key(raw),
        key_prefix=raw[:14],
        key_full=_encode_key(raw),
        credit_limit=body.credit_limit,
        unlimited=1 if body.unlimited else 0,
        note=body.note,
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    # 明文 key 仅此返回一次
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "credit_limit": k.credit_limit,
        "unlimited": bool(k.unlimited),
    }


@router.get("/{key_id}/view")
def view_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """查看 Key 完整值（创建后可再次查看）。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    raw = _decode_key(k.key_full or "")
    if not raw:
        raise HTTPException(status_code=410, detail="该 Key 创建于「查看功能」上线前，完整值未存储。建议重新生成一个新 Key。")
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/{key_id}/usage")
def key_usage(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """查看 Key 的积分额度消耗情况。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    # credit_limit / credit_used 的列定义允许 NULL（旧数据 / 手工插入），
    # 直接做算术会抛 TypeError，把整个端点变成 500。统一把 NULL 视为 0：
    # 与 ORM 建行时的 default=0 语义一致。
    limit = k.credit_limit or 0
    used = k.credit_used or 0
    pct = ((used / limit) * 100) if limit > 0 and not k.unlimited else None
    return {
        "id": k.id,
        "name": k.name,
        "credit_limit": k.credit_limit,
        "credit_used": round(used, 2),
        "credit_remaining": (round(max(0, limit - used), 2) if not k.unlimited else None),
        "usage_percent": round(pct, 1) if pct is not None else None,
        "unlimited": bool(k.unlimited),
        "status": k.status,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.patch("/{key_id}")
def patch_key(key_id: int, body: dict, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    if "name" in body:
        k.name = body["name"]
    if "credit_limit" in body:
        try:
            k.credit_limit = float(body["credit_limit"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="credit_limit 不是合法数字")
    if "unlimited" in body:
        k.unlimited = 1 if body["unlimited"] else 0
    if "status" in body and body["status"] in ("active", "revoked"):
        k.status = body["status"]
    if "note" in body:
        k.note = body["note"]
    if body.get("reset_used"):
        k.credit_used = 0
    db.commit()
    return {"id": k.id, "ok": True}


@router.delete("/{key_id}")
def delete_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    db.delete(k)
    db.commit()
    return {"id": key_id, "ok": True}
