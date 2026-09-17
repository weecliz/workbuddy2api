"""模型配置管理：获取可用模型列表 / 系统级&用户级白名单 / 启用禁用。

使用方式：
  1. 先调用 POST /api/models/sync 从后端拉取可用模型列表（自动入库）
  2. 在列表中启用/禁用特定模型
  3. 代理转发时自动过滤不在白名单内的模型
"""

import json
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from admin import backend
from admin.config import settings
from admin.db import get_db
from admin.models import Account, ModelConfig
from admin.security import require_admin

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelToggleIn(BaseModel):
    enabled: bool = True


class ModelBatchIn(BaseModel):
    # 留空 = 该 level 下的全部模型（前端「全部启用 / 全部禁用」按钮就是不带
    # model_ids 调用的）；给了具体列表则只更新这些模型。
    model_ids: list[str] = []
    enabled: bool = True
    level: str = "system"  # system | user


def _get_enabled_models(db: Session) -> set[str]:
    """获取当前启用的模型 ID 集合（用于代理过滤）。"""
    rows = db.query(ModelConfig).filter(ModelConfig.enabled == 1).all()
    return {m.model_id for m in rows}


def _get_free_models(db: Session) -> set[str]:
    """获取当前启用且免费的模型 ID 集合（credit_multiplier == 0）。"""
    rows = (
        db.query(ModelConfig)
        .filter(
            ModelConfig.enabled == 1,
            (ModelConfig.credit_multiplier == 0)
            | (ModelConfig.credit_multiplier.is_(None)),
        )
        .all()
    )
    return {m.model_id for m in rows}


def _is_model_allowed(db: Session, model_id: str) -> bool:
    """检查模型是否在白名单内。没有任何配置时放行全部（向后兼容）。"""
    configs = db.query(ModelConfig).all()
    if not configs:
        return True  # 未配置任何模型规则，放行
    enabled = {m.model_id for m in configs if m.enabled == 1}
    if not enabled:
        return True  # 全部禁用视为未配置，放行
    return model_id in enabled


@router.get("/configs")
def list_configs(
    level: str | None = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """列出所有模型配置（启用的在前，再按倍率从低到高）。

    COALESCE 兜底 NULL：两列都可为空，而各方言对 NULL 的排序位置不一致。
    """
    q = db.query(ModelConfig)
    if level:
        q = q.filter(ModelConfig.level == level)
    rows = q.order_by(
        func.coalesce(ModelConfig.enabled, 0).desc(),
        func.coalesce(ModelConfig.credit_multiplier, 0).asc(),
        ModelConfig.id.asc(),
    ).all()
    return {
        "items": [
            {
                "id": m.id,
                "level": m.level,
                "model_id": m.model_id,
                "enabled": bool(m.enabled),
                "note": m.note,
                "credit_multiplier": m.credit_multiplier or 0,
                "credits_raw": m.credits_raw or "",
                "is_free": (m.credit_multiplier or 0) == 0,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


def _do_sync_models(db: Session) -> dict:
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。

    需要一个有效账号来调用后端 API；优先选 active 且余额 > 0 的账号。
    """
    acc = (
        db.query(Account)
        .filter(Account.status == "active", Account.balance_remain > 0)
        .first()
    )
    if not acc:
        raise HTTPException(
            status_code=503, detail="无可用账号（无法连接后端获取模型列表）"
        )
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            models_raw = sess.fetch_models()
            acc.auth_json = sess.updated_json()
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取模型列表失败: {e}")

    import re

    added = 0
    updated = 0
    seen = {m.model_id for m in db.query(ModelConfig.model_id).all()}
    for m in models_raw:
        mid = m.get("id")
        if not mid or mid.lower() == "auto":
            continue  # 跳过上游聚合模型 "auto"，我们系统有自己的免费优先 auto 逻辑
        # 解析积分倍率
        raw_credits = m.get("credits") or ""
        multiplier = 0.0
        if raw_credits:
            cre_match = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(raw_credits))
            if cre_match:
                try:
                    multiplier = float(cre_match.group(1))
                except (ValueError, TypeError):
                    pass
        if mid in seen:
            # 已存在：覆盖倍率 / 原始 credits / 备注（保留用户启停状态）
            mc = db.query(ModelConfig).filter(ModelConfig.model_id == mid).first()
            if mc:
                mc.credit_multiplier = multiplier
                mc.credits_raw = str(raw_credits)
                if not mc.note:
                    mc.note = m.get("name") or ""
                updated += 1
            continue
        seen.add(mid)
        mc = ModelConfig(
            level="system",
            model_id=mid,
            enabled=1,
            note=m.get("name") or "",
            credit_multiplier=multiplier,
            credits_raw=str(raw_credits),
        )
        db.add(mc)
        added += 1
    db.commit()
    total = db.query(ModelConfig).count()
    enabled_count = db.query(ModelConfig).filter(ModelConfig.enabled == 1).count()
    return {
        "total_fetched": len(models_raw),
        "added": added,
        "updated": updated,
        "total_in_db": total,
        "enabled": enabled_count,
    }


@router.post("/sync")
def sync_models(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。"""
    return _do_sync_models(db)


@router.patch("/{config_id}")
def toggle_model(
    config_id: int,
    body: ModelToggleIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """启用/禁用某个模型。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mc.enabled = 1 if body.enabled else 0
    if body.enabled and mc.note:
        pass  # keep existing note
    db.commit()
    return {"id": mc.id, "model_id": mc.model_id, "enabled": bool(mc.enabled)}


@router.post("/batch-toggle")
def batch_toggle(
    body: ModelBatchIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量启用/禁用模型。

    body.model_ids 为空时作用于该 level 下的全部模型（「全部启用 / 全部禁用」按钮
    走的就是这条路）；给了列表则只更新列表内的模型。

    历史坑：这里原先只按 model_ids 循环，而前端按钮从不传该字段，导致循环体一次
    都不执行、恒返回 updated=0 —— 按钮点了没反应，但 HTTP 是 200，很难被发现。
    """
    q = db.query(ModelConfig).filter(ModelConfig.level == body.level)
    if body.model_ids:
        q = q.filter(ModelConfig.model_id.in_(body.model_ids))

    target = 1 if body.enabled else 0
    count = 0
    for mc in q.all():
        if mc.enabled != target:
            mc.enabled = target
            count += 1
    db.commit()
    return {"updated": count, "enabled": body.enabled, "level": body.level}


@router.delete("/{config_id}")
def delete_config(
    config_id: int,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除某条模型配置（从白名单移除 = 禁用该模型）。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mid = mc.model_id
    db.delete(mc)
    db.commit()
    return {"id": config_id, "model_id": mid, "ok": True}


# ---------------------------------------------------------------------------
# 测速：用账号池直接对上游发请求，免 API Key
# ---------------------------------------------------------------------------

_SPEED_TEST_PROMPT = [{"role": "user", "content": "用一句话介绍一下你自己。"}]


@router.post("/speed-test")
async def speed_test(
    body: dict,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """用账号池直接对上游发一次流式请求测速（无需 API Key）。

    测量指标：首字延迟 TTFT、输出 token 数、生成速度(t/s)、端到端耗时。
    仅占用一个可用账号做一次真实上游请求，不扣积分、不改额度。
    """
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="缺少 model")
    try:
        max_tokens = int(body.get("max_tokens") or 256)
    except (TypeError, ValueError):
        max_tokens = 256
    messages = body.get("messages") or _SPEED_TEST_PROMPT

    # 选可用账号（与代理一致：active 且余额>0，剩余最多优先）
    acc = (
        db.query(Account)
        .filter(Account.status == "active", Account.balance_remain > 0)
        .order_by(Account.balance_remain.desc())
        .first()
    )
    if not acc:
        raise HTTPException(status_code=503, detail="无可用账号（全部禁用或额度耗尽）")

    sess = backend.AccountSession(acc.auth_json)
    headers = sess.get_headers()
    url = f"{settings.BACKEND}/v2/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }

    t0 = time.perf_counter()
    ttft = None
    completion_tokens = None
    total_tokens = None
    prompt_tokens = None
    sample = ""
    sample_len = 0

    def _persist():
        try:
            acc.auth_json = sess.updated_json()  # 写回可能刷新的 token
            db.commit()
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=120, limits=backend.HTTP_LIMITS) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    detail = await r.aread()
                    msg = detail.decode("utf-8", "replace")[:300]
                    _persist()
                    sess.close()
                    raise HTTPException(
                        status_code=r.status_code, detail=f"上游返回错误: {msg}"
                    )
                async for chunk in r.aiter_text():
                    # TTFT：首个非空增量（content 或 reasoning_content）即首字延迟
                    if ttft is None and chunk.strip():
                        ttft = time.perf_counter() - t0
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if not p or p == "[DONE]":
                            continue
                        try:
                            obj = json.loads(p)
                        except Exception:
                            continue
                        usage = obj.get("usage")
                        if isinstance(usage, dict):
                            if usage.get("prompt_tokens") is not None:
                                prompt_tokens = usage["prompt_tokens"]
                            if usage.get("completion_tokens") is not None:
                                completion_tokens = usage["completion_tokens"]
                            if usage.get("total_tokens") is not None:
                                total_tokens = usage["total_tokens"]
                        for ch in obj.get("choices") or []:
                            delta = ch.get("delta") or {}
                            # 推理类模型把思考内容放在 reasoning_content（content 可能为空），
                            # 样例优先取 content，缺省回退 reasoning_content，保证总能看到输出。
                            piece = (delta.get("content") or "") or (
                                delta.get("reasoning_content") or ""
                            )
                            if piece and sample_len < 400:
                                add = piece[: 400 - sample_len]
                                sample += add
                                sample_len += len(add)
            t_end = time.perf_counter()
    except HTTPException:
        raise
    except Exception as e:
        _persist()
        sess.close()
        raise HTTPException(status_code=502, detail=f"测速请求失败: {e}")

    _persist()
    sess.close()

    total_dur = t_end - t0
    gen_dur = (total_dur - ttft) if ttft is not None else total_dur
    gen_dur = max(gen_dur, 0.0)
    speed_tps = (
        round(completion_tokens / gen_dur, 2)
        if completion_tokens and gen_dur > 0
        else None
    )
    overall_tps = (
        round(completion_tokens / total_dur, 2)
        if completion_tokens and total_dur > 0
        else None
    )
    ttft_ms = round(ttft * 1000, 1) if ttft is not None else None

    meta = backend.parse_auth_meta(acc.auth_json)
    return {
        "model": model,
        "account": meta.get("nickname") or meta.get("uid") or "",
        "ttft_ms": ttft_ms,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "gen_duration_ms": round(gen_dur * 1000, 1),
        "total_duration_ms": round(total_dur * 1000, 1),
        "speed_tps": speed_tps,
        "overall_tps": overall_tps,
        "sample": sample,
    }
