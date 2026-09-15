"""上游请求用量：拉取真实用量并聚合（对接 WorkBuddy 自有接口，不自建日志）。

上游端点 `POST /billing/meter/get-user-request-usage`（注意：**没有** /v2 前缀）
按「账号 × 时间区间」查询，语义已实测确认（2026-09-15）：

  - 入参 `startTime` / `endTime` 为 `YYYY-MM-DD HH:MM:SS`，**任意区间均可**，不限于当天；
  - 返回 `{"code":0,"msg":"OK","data":{"total":N,"data":[...]}}`，`total` 是区间内总条数；
  - 分页按 `requestTime` **降序**排列，页间**不重复**；
  - `pageSize` 实测可一次性返回 500 条以上（无硬上限），故默认按 `_PAGE_SIZE=500` 翻页拉全量；
  - 空区间返回 `{"total":0,"data":[]}`，不报错。

单条记录字段（响应式命名，勿改成 snake_case，前端与上游对齐）：

  requestId / credit / model / client / requestTime / inputTrunc / input / agentPurpose

**为什么在这里聚合**：上游只有单账号维度，没有跨账号汇总接口。号池里 N 个账号
就得发 N 组请求，因此本模块只做「单账号拉全量 + 纯函数式聚合」，并发与缓存交给
调用方（`admin/routers/usage.py`），便于单测直接喂假数据、不碰网络。
"""
from datetime import datetime, timedelta

# 上游单页上限（实测 500 可整包返回）。取 500 而不是 100，是为了让
# 「单账号单日 ~500 条」这类常规查询一次到位，省掉一次往返。
_PAGE_SIZE = 500
# 分页保护：正常单日量级远低于此；超过则说明区间过大，宁可截断也不无限翻页
# 把上游和本进程拖死。截断时会置 truncated=True，由调用方提示用户。
_MAX_PAGES = 40


def to_float(value) -> float:
    """把上游字段安全转成浮点数（上游是外部数据，可能为 None / 字符串 / 异常值）。

    上游积分是小数（如 0.25），历史实现里出现过字符串形式，统一在此收敛；
    真值缺失或非法按 0 计，不抛异常——统计页面缺一条总额，但不能因此整页失败。
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def to_int(value) -> int:
    """把外部字段安全转成整数（语义同 to_float，非法值按 0 计）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fetch_usage_range(sess, start_time: str, end_time: str,
                      page_size: int = _PAGE_SIZE,
                      max_pages: int = _MAX_PAGES) -> dict:
    """拉取某账号在 [start_time, end_time] 内的全量用量记录。

    返回 {"records": [...], "total": int, "pages": int, "truncated": bool}：
      - records：原始记录列表（上游字段原样保留，不裁剪）；
      - total：上游报告的区间总条数（用于校验是否拉全）；
      - truncated：达到 max_pages 仍未拉完时为 True。

    翻页在「本页返回条数 < page_size」或「累计条数 >= total」时停止；
    上游若返回空页也立即停止，避免死循环。
    """
    records: list[dict] = []
    total = 0
    pages = 0
    for page in range(1, max_pages + 1):
        resp = sess.fetch_request_usage(start_time, end_time, page, page_size)
        data = resp.get("data") or {}
        rows = data.get("data") or []
        if not isinstance(rows, list):
            rows = []
        # total 可能出现在 data.total（实测口径），兜底取顶层
        if not total:
            total = to_int(data.get("total") or resp.get("total"))
        pages = page
        records.extend(r for r in rows if isinstance(r, dict))
        if len(rows) < page_size:
            break
        if total and len(records) >= total:
            break
    truncated = bool(total and len(records) < total)
    return {"records": records, "total": total, "pages": pages, "truncated": truncated}


def _parse_ts(value: str) -> datetime | None:
    """解析上游 requestTime（`YYYY-MM-DD HH:MM:SS`）；解析失败返回 None 而非抛异常。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def aggregate_records(records: list[dict], start: datetime | None = None,
                      end: datetime | None = None) -> dict:
    """把原始记录聚合成汇总 / 按天 / 按模型三类结果（纯函数，无副作用）。

    只统计落在 [start, end] 内的记录（闭区间）——上游按账号时区返回，边界上偶有
    越界行，本地再夹一次可保证「按天分桶」与「区间总量」自洽。
    时间不可解析的记录仍计入总量，但不进按天分桶（否则会被塞进错误日期）。
    """
    summary = {"requests": 0, "credits": 0.0, "models": {}, "clients": {}, "purposes": {}}
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    latest = ""
    earliest = ""

    for r in records:
        ts = _parse_ts(str(r.get("requestTime") or ""))
        if start and ts and ts < start:
            continue
        if end and ts and ts > end:
            continue

        credit = to_float(r.get("credit"))
        model = str(r.get("model") or "未知")
        client = str(r.get("client") or "-")
        purpose = str(r.get("agentPurpose") or "-")

        summary["requests"] += 1
        summary["credits"] += credit
        summary["models"][model] = summary["models"].get(model, 0) + 1
        summary["clients"][client] = summary["clients"].get(client, 0) + 1
        summary["purposes"][purpose] = summary["purposes"].get(purpose, 0) + 1

        raw_ts = str(r.get("requestTime") or "")
        if raw_ts:
            if not latest or raw_ts > latest:
                latest = raw_ts
            if not earliest or raw_ts < earliest:
                earliest = raw_ts

        if ts is not None:
            day = ts.strftime("%Y-%m-%d")
            d = by_day.setdefault(day, {"date": day, "requests": 0, "credits": 0.0})
            d["requests"] += 1
            d["credits"] += credit

        m = by_model.setdefault(model, {"model": model, "requests": 0, "credits": 0.0})
        m["requests"] += 1
        m["credits"] += credit

    summary["credits"] = round(summary["credits"], 6)
    return {
        "summary": summary,
        "by_day": sorted(
            ({**d, "credits": round(d["credits"], 6)} for d in by_day.values()),
            key=lambda x: x["date"],
        ),
        "by_model": sorted(
            ({**m, "credits": round(m["credits"], 6)} for m in by_model.values()),
            key=lambda x: x["credits"],
            reverse=True,
        ),
        "latest_request_at": latest,
        "earliest_request_at": earliest,
    }


def merge_aggregates(parts: list[dict]) -> dict:
    """合并多个账号的聚合结果（用于号池汇总）。

    各账号的 by_day / by_model 按维度累加，summary 直接相加；时间边界取全局
    最早 / 最晚。传入项来自 aggregate_records，结构一致，故无需再做兼容处理。
    """
    merged = {
        "summary": {"requests": 0, "credits": 0.0, "models": {}, "clients": {}, "purposes": {}},
        "days": {},
        "models": {},
        "latest_request_at": "",
        "earliest_request_at": "",
    }
    for p in parts:
        s = p.get("summary") or {}
        merged["summary"]["requests"] += to_int(s.get("requests"))
        merged["summary"]["credits"] += to_float(s.get("credits"))
        for field in ("models", "clients", "purposes"):
            for k, v in (s.get(field) or {}).items():
                merged["summary"][field][k] = merged["summary"][field].get(k, 0) + to_int(v)
        for d in p.get("by_day") or []:
            day = d.get("date")
            if not day:
                continue
            cur = merged["days"].setdefault(day, {"date": day, "requests": 0, "credits": 0.0})
            cur["requests"] += to_int(d.get("requests"))
            cur["credits"] += to_float(d.get("credits"))
        for m in p.get("by_model") or []:
            model = m.get("model")
            if not model:
                continue
            cur = merged["models"].setdefault(model, {"model": model, "requests": 0, "credits": 0.0})
            cur["requests"] += to_int(m.get("requests"))
            cur["credits"] += to_float(m.get("credits"))
        for field in ("latest_request_at", "earliest_request_at"):
            v = p.get(field) or ""
            if not v:
                continue
            cur_v = merged[field]
            if not cur_v:
                merged[field] = v
            elif field == "latest_request_at":
                merged[field] = max(cur_v, v)
            else:
                merged[field] = min(cur_v, v)

    merged["summary"]["credits"] = round(merged["summary"]["credits"], 6)
    merged["by_day"] = sorted(
        ({**d, "credits": round(d["credits"], 6)} for d in merged["days"].values()),
        key=lambda x: x["date"],
    )
    merged["by_model"] = sorted(
        ({**m, "credits": round(m["credits"], 6)} for m in merged["models"].values()),
        key=lambda x: x["credits"],
        reverse=True,
    )
    for k in ("days", "models"):
        merged.pop(k, None)
    return merged


def normalize_range(start_time: str, end_time: str,
                    now: datetime | None = None) -> tuple[str, str, datetime, datetime]:
    """规范化查询区间，返回 (start_str, end_str, start_dt, end_dt)。

    - 两端都为空：默认**当天**（00:00:00 ~ 23:59:59）；
    - 只给日期（`YYYY-MM-DD`）自动补全时分秒；
    - 结束早于开始时自动交换，避免拿到空结果却看不出原因；
    - 上限 `_MAX_RANGE_DAYS` 天，超出则截断起始日（防一次查询翻太多页）。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")

    def _fill(value: str, end: bool) -> str:
        v = (value or "").strip()
        if not v:
            return f"{today} {'23:59:59' if end else '00:00:00'}"
        if len(v) <= 10:  # 只给了日期
            return f"{v} {'23:59:59' if end else '00:00:00'}"
        return v

    a, b = start_time, end_time
    s_str, e_str = _fill(a, False), _fill(b, True)
    s_dt = _parse_ts(s_str) or now.replace(hour=0, minute=0, second=0, microsecond=0)
    e_dt = _parse_ts(e_str) or now
    if e_dt < s_dt:
        # 区间颠倒时**交换原始入参后重新补时分秒**，而不是交换已解析的结果——
        # 后者会得到 start=23:59:59 / end=00:00:00，几乎丢掉整个区间。
        a, b = b, a
        s_str, e_str = _fill(a, False), _fill(b, True)
        s_dt = _parse_ts(s_str) or s_dt
        e_dt = _parse_ts(e_str) or e_dt
    max_days = 366
    if (e_dt - s_dt) > timedelta(days=max_days):
        s_dt = e_dt - timedelta(days=max_days)
        s_str = s_dt.strftime("%Y-%m-%d %H:%M:%S")
    return s_str, e_str, s_dt, e_dt
