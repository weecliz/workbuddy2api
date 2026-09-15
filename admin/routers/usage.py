"""用量统计：从上游拉取**真实**用量，按任意区间汇总 / 分桶 / 下钻。

与 `admin/routers/logs.py` 的区别（两者互补，别混）：

  - `logs.py` 读本地 `usage_logs` 表，是**网关自己的记账**（估算或事后回写），
    只覆盖经过本网关的调用；
  - 本模块读**上游真实账单**（`/billing/meter/get-user-request-usage`），
    包含桌面端直连等一切走该账号的请求，是权威口径。

设计要点：

  - **区间不写死当天**：`start` / `end` 接受任意日期或完整时间戳，缺省才是当天；
  - 上游只有单账号维度，号池汇总是「按账号并发拉取后 merge」；
  - 底层 `_request_backend` 是同步 httpx，故用线程池并发（而非 async），
    否则 N 个账号会串行阻塞；
  - 原始记录与聚合结果成对缓存 60s：上游用量本身有分钟级延迟，缓存不会让数据
    「更旧」，却能让反复查看与切换维度秒回、不打爆上游；
  - 单个账号失败只记录在 `errors` 里，不拖垮整次查询（部分账号凭据过期是常态）。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from admin import backend
from admin.backend.usage import to_float, to_int
from admin.db import get_db
from admin.models import Account
from admin.security import require_admin

router = APIRouter(prefix="/api/usage", tags=["usage"])

_logger = logging.getLogger("usage")

# 并发拉取上限：上游是外部服务，号池再多也不能无节制开线程。
_MAX_WORKERS = 8
# 单次查询最多覆盖的账号数（防误操作把整池几百个账号全查一遍）
_MAX_ACCOUNTS = 50
# 短缓存时长（秒）。上游用量有 30~90s 延迟，缓存 60s 不会损失新鲜度。
_CACHE_TTL_S = 60
# 缓存条目上限：超出后淘汰最旧条目，防长时间运行后内存无界增长。
_CACHE_MAX = 64

# key -> (写入时间戳, 值)。两种值：汇总 payload / 单账号 {records, agg}
_cache: "dict[str, tuple[float, dict]]" = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> dict | None:
    """取缓存（过期即视为未命中并顺手清除）。"""
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > _CACHE_TTL_S:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: dict) -> None:
    """写缓存，并淘汰最旧条目以维持上限。"""
    with _cache_lock:
        _cache[key] = (time.time(), value)
        while len(_cache) > _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)


def _fetch_one(acc_id: int, acc_name: str, auth_json: str, start: str, end: str) -> dict:
    """拉取单个账号的区间用量，返回 {ok, records, agg, error, ...}（不抛异常）。

    每次都用独立会话：`AccountSession` 会把凭据落临时文件、关闭时读回
    （token 可能被刷新），跨线程共享会话实例不安全。
    """
    out: dict = {"account_id": acc_id, "account_name": acc_name, "ok": False,
                 "error": "", "records": [], "agg": None, "truncated": False}
    sess = None
    try:
        sess = backend.AccountSession(auth_json)
        raw = backend.fetch_usage_range(sess, start, end)
        out["records"] = raw["records"]
        out["agg"] = backend.aggregate_records(raw["records"])
        out["truncated"] = bool(raw["truncated"])
        out["ok"] = True
    except Exception as e:
        # 单账号失败（凭据过期 / 上游抖动）不该让整个统计页报错
        out["error"] = str(e)[:300]
        _logger.warning("用量查询失败 acc=%s(%s): %s", acc_id, acc_name, e)
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
    return out


def _collect(accounts: list[Account], start: str, end: str, force: bool = False) -> list[dict]:
    """并发拉取多个账号（按入参顺序返回结果）。每个账号先查缓存。

    force=True 时**跳过读写缓存**，强制回上游重拉——「强制刷新」按钮靠它生效；
    否则会命中上一次的单账号缓存，看着像刷新了其实拿的是旧数据。
    每条结果带 from_cache 标记，便于调用方告知前端是否为缓存命中。
    """
    todo: list[tuple[Account, dict | None]] = []
    for acc in accounts:
        cached = None if force else _cache_get(f"acc|{start}|{end}|{acc.id}")
        todo.append((acc, cached))

    pending = [(a, c) for a, c in todo if c is None]
    fetched: dict[int, dict] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(pending))) as pool:
            futures = {
                pool.submit(_fetch_one, a.id, str(a.name or f"#{a.id}"),
                            str(a.auth_json), start, end): a.id
                for a, _ in pending
            }
            for fut, acc_id in futures.items():
                res = fut.result()
                fetched[acc_id] = res
                # 失败不入缓存：凭据过期往往是临时状态，下次应重试而不是回放旧错误
                if res["ok"] and not force:
                    _cache_put(f"acc|{start}|{end}|{acc_id}",
                               {"records": res["records"], "agg": res["agg"],
                                "truncated": res["truncated"]})

    results: list[dict] = []
    for acc, cached in todo:
        if cached is not None:
            agg = cached.get("agg")
            results.append({
                "account_id": acc.id,
                "account_name": str(acc.name or f"#{acc.id}"),
                "ok": agg is not None,
                "error": "",
                "records": cached.get("records") or [],
                "agg": agg,
                "truncated": bool(cached.get("truncated")),
                "from_cache": True,
            })
        else:
            results.append({**fetched[acc.id], "from_cache": False})
    return results


def _accounts_for(db: Session, account_id: Optional[int]) -> list[Account]:
    """确定要查询的账号集合：指定 id 就查单个，否则查全部 active 账号。"""
    if account_id:
        acc = db.query(Account).filter(Account.id == account_id).first()
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        return [acc]
    return list(db.query(Account)
                .filter(Account.status == "active")
                .order_by(Account.id)
                .limit(_MAX_ACCOUNTS)
                .all())


def _account_brief(res: dict) -> dict:
    """把内部结果整理成给前端的单账号摘要（不含 records，避免响应体暴涨）。"""
    agg = res.get("agg") or {}
    summary = agg.get("summary") or {}
    return {
        "account_id": res["account_id"],
        "account_name": res["account_name"],
        "ok": bool(res["ok"]),
        "error": res.get("error") or "",
        "requests": to_int(summary.get("requests")),
        "credits": round(to_float(summary.get("credits")), 6),
        "truncated": bool(res.get("truncated")),
        "earliest_request_at": agg.get("earliest_request_at") or "",
        "latest_request_at": agg.get("latest_request_at") or "",
    }


@router.get("/summary")
def usage_summary(
    start: Optional[str] = None,
    end: Optional[str] = None,
    account_id: Optional[int] = None,
    refresh: bool = False,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """区间用量汇总：总量 + 按天 + 按模型 + 分账号占比。

    - `start` / `end`：`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`，**任意区间**，
      缺省为当天；结束早于开始时自动交换；
    - `account_id`：只查该账号；不传则汇总全部 active 账号；
    - `refresh=true`：绕过 60s 短缓存强制拉取。
    """
    s_str, e_str, _s_dt, _e_dt = backend.normalize_range(start or "", end or "")
    accounts = _accounts_for(db, account_id)
    if not accounts:
        return {"start": s_str, "end": e_str, "account_count": 0, "cached": False,
                "summary": {"requests": 0, "credits": 0.0, "models": {},
                            "clients": {}, "purposes": {}},
                "by_day": [], "by_model": [], "accounts": [], "errors": [],
                "partial": False}

    ids = ",".join(str(a.id) for a in accounts)
    cache_key = f"summary|{s_str}|{e_str}|{ids}"
    if not refresh:
        hit = _cache_get(cache_key)
        if hit is not None:
            return {**hit, "cached": True}

    results = _collect(accounts, s_str, e_str, force=refresh)
    ok_parts = [r["agg"] for r in results if r.get("agg")]
    merged = backend.merge_aggregates(ok_parts)
    errors = [{"account_id": r["account_id"], "account_name": r["account_name"],
               "error": r["error"]} for r in results if not r["ok"]]

    payload = {
        "start": s_str,
        "end": e_str,
        "account_count": len(accounts),
        "cached": bool(results) and all(r.get("from_cache") for r in results),
        "summary": merged["summary"],
        "by_day": merged["by_day"],
        "by_model": merged["by_model"],
        "accounts": sorted((_account_brief(r) for r in results),
                           key=lambda x: x["credits"], reverse=True),
        "errors": errors,
        "partial": bool(errors) and bool(ok_parts),
    }
    _cache_put(cache_key, payload)
    return payload


@router.get("/detail")
def usage_detail(
    start: Optional[str] = None,
    end: Optional[str] = None,
    account_id: Optional[int] = None,
    model: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    refresh: bool = False,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """区间用量明细（按 `requestTime` 降序，服务端分页）。

    `account_id` 省略时查全部 active 账号并在本地合并：跨账号无法在上游层面
    分页，故先全量拉取再切片；原始记录命中 60s 缓存，重复翻页不会重复请求上游。
    """
    s_str, e_str, _s_dt, _e_dt = backend.normalize_range(start or "", end or "")
    accounts = _accounts_for(db, account_id)
    if not accounts:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "start": s_str, "end": e_str, "errors": [], "cached": False}

    results = _collect(accounts, s_str, e_str, force=refresh)
    rows: list[dict] = []
    for res in results:
        for r in res.get("records") or []:
            rows.append({
                "account_id": res["account_id"],
                "account_name": res["account_name"],
                "requestId": r.get("requestId") or "",
                "credit": r.get("credit"),
                "model": r.get("model") or "",
                "client": r.get("client") or "",
                "requestTime": r.get("requestTime") or "",
                "agentPurpose": r.get("agentPurpose") or "",
                "input": r.get("inputTrunc") or r.get("input") or "",
            })

    if model:
        kw = model.strip().lower()
        rows = [r for r in rows if kw in str(r["model"]).lower()]
    rows.sort(key=lambda r: str(r["requestTime"]), reverse=True)

    total = len(rows)
    offset = (page - 1) * page_size
    errors = [{"account_id": r["account_id"], "account_name": r["account_name"],
               "error": r["error"]} for r in results if not r["ok"]]
    return {
        "items": rows[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "start": s_str,
        "end": e_str,
        "errors": errors,
        "cached": bool(results) and all(r.get("from_cache") for r in results),
    }


@router.post("/cache/clear")
def clear_cache(_: bool = Depends(require_admin)):
    """清空用量缓存（排查数据不一致时用）。"""
    with _cache_lock:
        _cache.clear()
    return {"ok": True, "message": "已清空用量缓存"}


@router.get("/accounts")
def usage_accounts(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """可查询的账号列表（给前端下拉框用，避免拉整表）。"""
    rows = db.query(Account).order_by(Account.id).all()
    return {"items": [{
        "id": a.id,
        "name": str(a.name or f"#{a.id}"),
        "status": str(a.status or ""),
        "balance_remain": to_int(a.balance_remain),
    } for a in rows]}
