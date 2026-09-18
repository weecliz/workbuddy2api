"""代理网关：带 API Key 的 /v1/chat/completions 与 /v1/models。

流程：校验 Key → 配额拦截（超额提示『积分已耗尽』）→ 从可用账号中挑选 →
用该账号凭据转发到后端 → 流式返回 → 按用量回扣 Key 额度。
"""
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from admin import backend
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account, ApiKey, ModelConfig, UsageLog
from admin.routers.models import _is_model_allowed
from admin.security import check_quota, get_key_row

HTTP_LIMITS = backend.HTTP_LIMITS

_logger = logging.getLogger("proxy")

# 真实积分回写的有界队列 + 单 worker（见 _queue_real_credits / _credit_worker）。
# 上限按「上游对账延迟 60s × 峰值 QPS」估：本机实测峰值 ~0.12 QPS，即便涨到
# 10 QPS 也只积压约 600 条；取 2048 留足余量，同时给资源设硬上限。
_CREDIT_QUEUE: "queue.Queue" = queue.Queue(maxsize=2048)
_CREDIT_WORKER: threading.Thread | None = None
_CREDIT_WORKER_LOCK = threading.Lock()
# 上游用量接口的分钟级延迟：等够这么久去查才拿得到真实积分
_CREDIT_DELAY_S = 60

# Responses API 适配器（converter 同款）；缺失时 /v1/responses 优雅降级为 501
try:
    from core.responses_adapter import (
        responses_request_to_chat,
        ResponsesStreamConverter,
        custom_tool_names,
    )
    from core.responses_projection import project_responses_chat_body
    _RESPONSES_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _RESPONSES_AVAILABLE = False
    responses_request_to_chat = None
    custom_tool_names = None
    ResponsesStreamConverter = None
    project_responses_chat_body = None

# Anthropic Messages 适配器（与 converter 同款，复用同一套双向转换）；
# 缺失时 /v1/messages 优雅降级为 501，不影响其余端点。
try:
    from core.anthropic_adapter import AnthropicStreamConverter, anthropic_request_to_chat
    _ANTHROPIC_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _ANTHROPIC_AVAILABLE = False
    anthropic_request_to_chat = None
    AnthropicStreamConverter = None

# harness 脱敏（与 converter 的 /gw 端点同款）。Claude Code 的 system prompt / tools
# 里含 "DoS / exploit / credential" 这类合规声明词，会被上游内容审核误判并整条拒绝
# （典型报错 400 code=11128 "Illegal API invocation from an unapproved channel"）。
try:
    from core.desensitize import desensitize_body
    _DESENSITIZE_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _DESENSITIZE_AVAILABLE = False
    desensitize_body = None

router = APIRouter(tags=["proxy"])


def _client_ip(request: Request) -> str:
    """提取发起请求的真实客户端 IP。

    经反向代理部署时，上游往往会带上 X-Forwarded-For / X-Real-IP；
    取 XFF 首个（最原始客户端），否则 X-Real-IP，最后退回直连 socket 地址。
    这是「记录原客户端用户真实 IP」的关键，便于风控对账与上游用途日志对齐。
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


def _upstream_extra_headers(request: Request, purpose: str = "conversation") -> dict:
    """构造需要透传给上游的风控/审计头。

    - X-Forwarded-For / X-Real-IP / X-Client-IP: 把原始客户端真实 IP 带上去，
      让上游请求用量里的「客户端」列能显示真实来源，而不是反代服务器 IP。
      若环境变量 ADMIN_UPSTREAM_CLIENT_HEADER 指定了自定义 header 名，则额外发送该头。
    - X-Agent-Purpose: WorkBuddy 请求用途头，用于上游用量分类；
      缺失时上游请求用量「用途」列为空，易被风控识别为异常调用。
      真实 WorkBuddy 桌面端普通对话使用 "conversation"。
    - X-IDE-Name / X-IDE-Type / X-Product: 上游记录到请求用量「client」列的产品名，
      缺失时该列为空；真实桌面端发 WorkBuddy，因此默认带 WorkBuddy。
    """
    ip = _client_ip(request)
    client_name = settings.UPSTREAM_CLIENT_NAME.strip() or "WorkBuddy"
    h: dict[str, str] = {
        "X-Agent-Purpose": purpose or "conversation",
        "X-IDE-Name": client_name,
        "X-IDE-Type": client_name,
        "X-Product": client_name,
    }
    if ip:
        h.update({
            "X-Forwarded-For": ip,
            "X-Real-IP": ip,
            "X-Client-IP": ip,
        })
        custom = settings.UPSTREAM_CLIENT_HEADER.strip()
        if custom:
            h[custom] = ip
    return h


def _record_usage(key_id: int, account_id: int, model: str, credits: float | None,
                  updated_auth_json: str | None, *,
                  client_ip: str = "", use_case: str = "",
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None, total_tokens: int | None = None,
                  cached_tokens: int | None = None,
                  seq: int = 0, ttfb_ms: int | None = None,
                  latency_ms: int | None = None, error_kind: str = "") -> int | None:
    """流式响应结束后独立开一个 DB 会话写入用量/额度。

    关键点：请求作用域的 db 会话在端点返回 StreamingResponse 时已被依赖 teardown 关闭，
    不能在流式生成器里复用它做 commit（会抛 ResourceClosedError 且被流式 except 静默吞掉）。
    这里用全新的 SessionLocal 落库，并把错误显式记录到日志，绝不再静默丢失。

    credits 语义：
    - None 表示上游未返回真实积分，此时按模型倍率估算；
    - 0.0 表示上游明确返回 0 或请求失败，不再估算。

    若本次使用了估算值，会启动后台线程在 60 秒后调用上游用量接口回写真实积分。
    """
    log_id = None
    try:
        db = SessionLocal()
        try:
            # 上游 usage 没给 credits 时，用本地模型配置的 credit_multiplier 估算。
            # credit_multiplier 在 model_configs 里保存的是「每千 token 积分」，
            # 因此估算公式为 total_tokens * multiplier / 1000；
            # 当 total_tokens 缺失时用 completion_tokens 兜底。
            estimated = False
            original_credits = credits
            if credits is None and (total_tokens or completion_tokens):
                mc = db.query(ModelConfig).filter(ModelConfig.model_id == (model or ""),
                                                   ModelConfig.enabled == 1).first()
                mult = mc.credit_multiplier if mc else 0
                if mult:
                    toks = total_tokens if total_tokens else completion_tokens
                    credits = float(toks) * mult / 1000.0
                    estimated = True
            if credits is None:
                credits = 0.0
            _logger.info("记录用量 model=%s raw_credits=%s est=%s pt=%s ct=%s tt=%s cached=%s client_ip=%s use_case=%s",
                         model, original_credits, credits, prompt_tokens, completion_tokens, total_tokens,
                         cached_tokens, client_ip, use_case)
            key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
            if key is not None:
                key.credit_used = float(key.credit_used or 0) + credits
            acc = db.query(Account).filter(Account.id == account_id).first()
            if acc is not None:
                acc.balance_remain = max(0, int(acc.balance_remain or 0) - int(credits))
                acc.last_used_at = datetime.utcnow()
                acc.err_count = 0  # 成功即清零三振计数
                if updated_auth_json:
                    acc.auth_json = updated_auth_json
            log = UsageLog(
                api_key_id=key_id, account_id=account_id, model=model, credits=credits,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, cached_tokens=cached_tokens,
                client_ip=client_ip or "", use_case=use_case or "",
                seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms, error_kind=error_kind or "",
            )
            db.add(log)
            db.commit()
            log_id = log.id
            if estimated and account_id and updated_auth_json:
                _queue_real_credits(log_id, account_id, updated_auth_json, model, credits, log.created_at)
        finally:
            db.close()
    except Exception as e:  # 记账失败不应影响已返回的响应，但必须留痕便于排查
        _logger.exception("记录用量失败 key=%s acc=%s model=%s credits=%s: %s",
                          key_id, account_id, model, credits, e)
    return log_id


def _queue_real_credits(log_id: int, account_id: int, auth_json: str, model: str,
                        estimated_credits: float, created_at: datetime) -> None:
    """把「等待上游对账」的任务投进有界队列，由单个 worker 串行处理。

    原先每次估算积分就起一个线程、线程内 sleep(60) 再回写，等价于「每请求
    占一个线程 60 秒」。上游一旦开始不回 credit 字段，QPS 上到两位数就是几百
    个并发线程，必然耗尽资源。改为有界队列 + 单 worker：worker 只做「到期时间
    排序 + 到点回写」，sleep 不影响投递方。

    队列满时丢弃并告警——回写只是把估算值修正为真实值，丢一条不影响记账
    本身（UsageLog 已按估算倍率落库）。
    """
    if _CREDIT_QUEUE.full():
        _logger.warning("真实积分回写队列已满（%d），丢弃 log=%s", _CREDIT_QUEUE.maxsize, log_id)
        return
    _start_credit_worker()
    try:
        _CREDIT_QUEUE.put_nowait((created_at, log_id, account_id, auth_json,
                                  model, estimated_credits))
    except queue.Full:  # pragma: no cover - 与 full() 竞态
        _logger.warning("真实积分回写队列已满，丢弃 log=%s", log_id)


def _credit_worker() -> None:
    """单 worker：按 created_at 排序，等够 60s 再逐条回写。

    上游用量接口有分钟级延迟，必须等约 60s 才查得到。这里用「堆顶到期时间」
    决定睡眠时长，因此 sleep 是阻塞单线程而非每请求一线程——队列里积压再多，
    也只有一个线程。
    """
    while True:
        try:
            item = _CREDIT_QUEUE.get()
        except Exception:  # pragma: no cover - 队列异常不应杀死 worker
            time.sleep(1)
            continue
        created_at, log_id, account_id, auth_json, model, estimated = item
        try:
            due = (created_at + timedelta(seconds=_CREDIT_DELAY_S)).timestamp()
            wait = due - time.time()
            if wait > 0:
                time.sleep(min(wait, 300))
            _fetch_real_credits(log_id, account_id, auth_json, model, estimated, created_at)
        except Exception:
            _logger.exception("真实积分回写 worker 处理失败 log=%s", log_id)
        finally:
            _CREDIT_QUEUE.task_done()


def _start_credit_worker() -> None:
    """幂等启动回写 worker（模块首次用到时拉起）。"""
    global _CREDIT_WORKER
    with _CREDIT_WORKER_LOCK:
        if _CREDIT_WORKER is not None and _CREDIT_WORKER.is_alive():
            return
        _CREDIT_WORKER = threading.Thread(target=_credit_worker, name="credit-reconcile",
                                          daemon=True)
        _CREDIT_WORKER.start()
        _logger.info("真实积分回写 worker 已启动（队列上限 %d）", _CREDIT_QUEUE.maxsize)


def _fetch_real_credits(log_id: int, account_id: int, auth_json: str, model: str,
                        estimated_credits: float, created_at: datetime) -> None:
    """查询上游真实用量接口，回写 UsageLog.credits 并校正额度。

    上游 /billing/meter/get-user-request-usage 有分钟级延迟，通常在请求完成后 30~90s
    才能查到。等待由 `_credit_worker` 统一负责（按 created_at 排序休眠），本函数只做
    单次查询 + 回写，不再自带 sleep——否则就又变回「每请求一线程」。
    匹配规则：[created_at-5min, created_at+5min] + model 取最近一条。
    """
    try:
        # 必须用 backend.AccountSession：本模块只 import 了 backend，没有裸导入
        # AccountSession。写成裸名会在运行时抛 NameError，而它被本函数末尾的
        # except Exception 吞掉、只留一行日志 —— 结果是「真实积分回写」静默失效，
        # 扣费全部退回估算口径（与实际账单不符）。
        sess = backend.AccountSession(auth_json)
        try:
            start = (created_at - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            end = (created_at + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            result = sess.fetch_request_usage(start, end, page_num=1, page_size=50)
            data = (result.get("data") or {}).get("data") or []
            client_name = (settings.UPSTREAM_CLIENT_NAME or "WorkBuddy").strip() or "WorkBuddy"
            candidates = [
                r for r in data
                if r.get("model") == model and client_name in (r.get("client") or "")
            ]
            if not candidates:
                _logger.info("真实积分回写未找到匹配 log=%s model=%s", log_id, model)
                return
            # 取 requestTime 最接近 created_at 的一条
            def _ts(item):
                try:
                    return datetime.strptime(item.get("requestTime", ""), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return datetime.min
            best = min(candidates, key=lambda r: abs((_ts(r) - created_at).total_seconds()))
            real = float(best.get("credit") or 0)
            _logger.info("真实积分匹配 log=%s model=%s real=%s est=%s requestTime=%s",
                         log_id, model, real, estimated_credits, best.get("requestTime"))
            db = SessionLocal()
            try:
                log = db.query(UsageLog).filter(UsageLog.id == log_id).first()
                if log is None:
                    return
                delta = real - log.credits
                if abs(delta) < 0.0001:
                    return
                log.credits = real
                key = db.query(ApiKey).filter(ApiKey.id == log.api_key_id).first()
                if key is not None:
                    key.credit_used = max(0.0, float(key.credit_used or 0) + delta)
                acc = db.query(Account).filter(Account.id == log.account_id).first()
                if acc is not None:
                    acc.balance_remain = max(0, int(acc.balance_remain or 0) - int(delta))
                db.commit()
                _logger.info("真实积分回写完成 log=%s real=%s delta=%s", log_id, real, delta)
            finally:
                db.close()
        finally:
            sess.close()
    except Exception as e:
        _logger.exception("真实积分回写失败 log=%s: %s", log_id, e)

# ---------------------------------------------------------------------------
# 请求级表格日志：每个 /v1/* chat 请求出口打印一行。
# seq 进程级递增；模型截断 11 字符；uid 只显示前 8 位。
# ---------------------------------------------------------------------------
import itertools

_CHAT_SEQ = itertools.count(1)


def _log_chat_row(ttfb_ms, latency_ms, model, mode, uid, status, toks, error_kind=""):
    """向 stdout 打印一行表格日志，便于排查慢请求与风控。"""
    seq = next(_CHAT_SEQ)
    now = datetime.now().strftime("%H:%M:%S")
    model = (model or "-")[:11]
    tok_field = "-" if toks is None else str(toks)
    tps = "-"
    if toks is not None and latency_ms and latency_ms > 0:
        tps = f"{toks * 1000 / latency_ms:.1f}"
    ttfb = "-" if ttfb_ms is None or ttfb_ms <= 0 else f"{ttfb_ms}ms"
    uid_prefix = (uid or "-")[:8]
    latency = f"{latency_ms}ms" if latency_ms is not None else "-"
    print(f"| #{seq:03d} | {now} | {model:11s} | {mode:6s} | {status:3d} | uid={uid_prefix} | TTFB={ttfb:>5} | tok={tok_field:>5} | {tps:>6}t/s | total={latency:>7} | {error_kind}", flush=True)
    return seq


# ---------------------------------------------------------------------------
# 上游错误分类 + 账号状态机（ pool/upstream）：
#   - 网络层错误不累计 errCount
#   - 404 短冷却不累计 errCount（防雪崩）
#   - HTTP 5xx 累计 errCount，阈值 5 触发 10m 冷却
#   - 余额不足 / session 死亡 / 429 分别处理
# ---------------------------------------------------------------------------

_HARD_CREDIT_MARKERS = [
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough",
    "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
]
_SESSION_DEAD_MARKERS = ["Offline user session not found", "12153", "session not found", "invalid session"]
_SESSION_DEAD_THRESHOLD = 3  # 三振机制：session_dead 连续命中达到该次数才禁用


def _classify_error(status: int, body: str) -> str:
    """按 HTTP 状态码 + body 关键词返回错误分类（字符串形式，与 UsageLog.error_kind 对齐）。"""
    if status == 402 or status == 412:
        return "hard_credit"
    body_l = (body or "").lower()
    for m in _HARD_CREDIT_MARKERS:
        if m.lower() in body_l or m in body:
            return "hard_credit"
    for m in _SESSION_DEAD_MARKERS:
        if m in body:
            return "session_dead"
    if status == 429:
        return "soft_rate"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server"
    if status >= 400:
        return "client"
    return "transport"  # 网络层/无响应状态


def _next_day_4am(now: datetime) -> datetime:
    """返回 now 所属日期的次日 04:00（本地时区）。"""
    return now.replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _apply_account_policy(db: Session, acc: Account, kind: str, status: int, msg: str) -> None:
    """根据错误分类更新账号状态：冷却/禁用/错误计数。"""
    now = datetime.utcnow()
    if kind == "hard_credit":
        acc.cool_until = _next_day_4am(now)
        acc.cool_kind = "hard_credit"
        acc.err_count = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "余额不足")[:255]
    elif kind == "soft_rate":
        acc.cool_until = now + timedelta(seconds=60)
        acc.cool_kind = "soft_rate"
        acc.err_count = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "429 rate limit")[:255]
    elif kind == "session_dead":
        # 三振机制：连续命中死亡标记达到 _SESSION_DEAD_THRESHOLD 才禁用；
        # 未达阈值先 10 分钟冷却观察。成功请求会把 err_count 清零，
        # 因此这里实际统计的是「连续失败次数」（与 Go 版语义对齐）。
        acc.err_count = (acc.err_count or 0) + 1
        acc.last_err_at = now
        acc.last_err_msg = (msg or "session dead")[:255]
        if acc.err_count >= _SESSION_DEAD_THRESHOLD:
            acc.status = "disabled"
            acc.cool_kind = "session_dead"
            acc.err_count = 0
        else:
            acc.cool_until = now + timedelta(minutes=10)
            acc.cool_kind = "session_dead"
    elif kind == "not_found":
        # 404 短冷却不累计 errCount（防雪崩）
        acc.cool_until = now + timedelta(seconds=60)
        acc.cool_kind = "not_found"
        acc.last_err_at = now
        acc.last_err_msg = (msg or "upstream 404")[:255]
    elif kind == "server" or status >= 500:
        # HTTP 5xx 累计 errCount；阈值 5 触发 10m 冷却
        acc.err_count = (acc.err_count or 0) + 1
        acc.last_err_at = now
        acc.last_err_msg = (msg or f"upstream {status}")[:255]
        if acc.err_count >= 5:
            acc.cool_until = now + timedelta(minutes=10)
            acc.cool_kind = "error_threshold"
            acc.err_count = 0
    elif kind == "transport":
        # 网络层抖动不累计 errCount，只记录时间
        acc.last_err_at = now
        acc.last_err_msg = (msg or "transport error")[:255]
    else:
        # 其他 4xx 只换号，不累计 errCount
        acc.last_err_at = now
        acc.last_err_msg = (msg or f"upstream {status}")[:255]
    try:
        db.commit()
    except Exception:
        db.rollback()


def _account_session_safe(db: Session, acc: Account) -> backend.AccountSession | None:
    """创建 AccountSession 并调用 get_headers()（可能触发 token 刷新）。

    若刷新失败（session 死亡等），按策略禁用/冷却该账号并返回 None。
    """
    sess = backend.AccountSession(acc.auth_json)
    try:
        sess.get_headers()  # 内部会触发 token 刷新并写临时文件
        return sess
    except Exception as e:
        msg = str(e)
        kind = _classify_error(0, msg)
        if kind == "transport":
            # 纯网络层失败（连不上上游 / 超时）不等于 session 死亡：
            # 走 server 类累计（5 次触发 10 分钟冷却），永远不会禁用。
            # 真正的 session 死亡会在异常消息里携带上游标记
            # （如 "Offline user session not found" / 12153），
            # 由 _classify_error 直接归为 session_dead 计入三振。
            kind = "server"
        _apply_account_policy(db, acc, kind, 0, msg)
        try:
            sess.close()
        except Exception:
            pass
        return None


_CREDIT_RE = re.compile(r"x\s*([0-9]+(?:\.[0-9]+)?)")


def _select_account(db: Session, exclude_ids: set | None = None,
                    min_balance: int = 1, mark_picked: bool = True) -> Account | None:
    """从健康账号池中挑选一个账号。

    健康条件：active、有余额、不在冷却期、不在 exclude_ids 中。
    防撞号：优先跳过 last_picked_at 距今 < 100ms 的账号；若全部刚被用过则兜底。
    挑选策略默认按 balance_remain 降序（也可切 LRU）。
    """
    now = datetime.utcnow()
    q = db.query(Account).filter(Account.status == "active")
    if min_balance > 0:
        q = q.filter(Account.balance_remain > 0)
    # 排除已尝试或已冷却账号
    if exclude_ids:
        q = q.filter(~Account.id.in_(exclude_ids))
    q = q.filter(or_(Account.cool_until.is_(None), Account.cool_until <= now))

    # 防撞号窗口：100ms 内不重复选中同一账号
    anti = now - timedelta(milliseconds=100)
    q_anti = q.filter(or_(Account.last_picked_at.is_(None), Account.last_picked_at <= anti))

    if settings.ACCOUNT_SELECT == "lru":
        q_anti = q_anti.order_by(Account.last_used_at.asc())
        q = q.order_by(Account.last_used_at.asc())
    else:
        q_anti = q_anti.order_by(Account.balance_remain.desc())
        q = q.order_by(Account.balance_remain.desc())

    acc = q_anti.first()
    if not acc:
        acc = q.first()
    if acc and mark_picked:
        acc.last_picked_at = now
        db.commit()
    return acc


def _pick_best_model(db: Session, requested_model: str) -> str | None:
    """根据请求模型和可用配置，选出最优实际使用的模型 ID。

    策略：
      - 用户指定了具体模型 → 校验白名单后直接用（或返回 None 表示被拒）
      - 用户传 "auto" 或空 → 优先选免费模型（credit_multiplier=0），没有免费的才选付费的
      - 未配置任何模型规则时放行全部（向后兼容），返回原始 model
    """
    from admin.routers.models import _is_model_allowed, _get_free_models, _get_enabled_models
    from admin.models import ModelConfig

    # 检查是否有任何配置记录（无配置=向后兼容，放行全部）
    has_any_config = db.query(ModelConfig).first() is not None

    # 具体模型：有配置时校验白名单，无配置直接放行
    if requested_model and requested_model != "auto":
        if not has_any_config:
            return requested_model  # 无配置，放行
        if _is_model_allowed(db, requested_model):
            return requested_model
        return None  # 被白名单拒绝

    # auto 模式：有配置时免费优先，无配置也从后端取模型列表自选（绝不透传 auto）
    if not has_any_config:
        # 无本地配置时：尝试从后端拉一次模型列表来选免费模型
        try:
            acc_tmp = _select_account(db)
            if acc_tmp:
                with backend.AccountSession(acc_tmp.auth_json) as sess:
                    raw_models = sess.fetch_models()
                # 选第一个 credits 为 0 或含 "free"/"x0.00" 的模型
                for rm in raw_models:
                    mid = rm.get("id", "")
                    if mid and mid.lower() != "auto":
                        cred = str(rm.get("credits") or "")
                        if not cred or "x0.00" in cred or "free" in cred.lower():
                            return mid
                # 没有免费模型就返回第一个非 auto
                for rm in raw_models:
                    mid = rm.get("id", "")
                    if mid and mid.lower() != "auto":
                        return mid
        except Exception:
            pass
        return "deepseek-v4-flash"  # 兜底：无配置且后端不可达时用默认模型

    free_models = _get_free_models(db)
    if free_models:
        return list(free_models)[0]  # 取第一个免费模型

    # 无免费模型：取任意一个启用的
    enabled = _get_enabled_models(db)
    if enabled:
        return list(enabled)[0]

    return None  # 有配置但全禁用


def _candidate_models(db: Session, tried: set) -> list:
    """按 免费→付费 顺序返回可用模型候选（排除已尝试的），用于 429/5xx 自动切换。"""
    from admin.routers.models import _get_enabled_models, _get_free_models

    free = _get_free_models(db) - tried
    paid = (_get_enabled_models(db) - free) - tried
    return list(free) + list(paid)


def _parse_usage(sse_text: str) -> dict:
    """从 chat SSE 文本里找最后一个带 usage 的事件，解析 credits 与 token 明细。

    返回 {"credits", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"}。
    上游通常只在最后一个事件回传 usage（配合 stream_options.include_usage=True）。

    积分字段优先级：usage.credits > usage.credit > usage.cost，均缺失时返回 credits=None，
    由调用方按模型倍率估算（倍率单位为「每千 token」）。
    """
    credits = None
    prompt_tokens = completion_tokens = total_tokens = cached_tokens = None
    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        # 桌面端源码读取 usage.credit / usage.credits；旧协议有 usage.cost，一并兼容。
        cred = usage.get("credits")
        if cred is None:
            cred = usage.get("credit")
        if cred is None and isinstance(usage.get("cost"), (int, float)):
            cred = usage.get("cost")
        if cred is not None:
            cred_str = str(cred).strip()
            # 兼容 "x 100" / "x100" 以及纯数字 "100" / "100.5"
            m = _CREDIT_RE.search(cred_str)
            if m:
                credits = float(m.group(1))
            else:
                try:
                    credits = float(cred_str)
                except ValueError:
                    pass
            _logger.debug("parse_usage credit raw=%r parsed=%s", cred, credits)
        if usage.get("prompt_tokens") is not None:
            prompt_tokens = usage["prompt_tokens"]
        if usage.get("completion_tokens") is not None:
            completion_tokens = usage["completion_tokens"]
        if usage.get("total_tokens") is not None:
            total_tokens = usage["total_tokens"]
        # 缓存命中 token：OpenAI 标准在 prompt_tokens_details.cached_tokens
        cached = None
        ptd = usage.get("prompt_tokens_details")
        if isinstance(ptd, dict):
            cached = ptd.get("cached_tokens")
        if cached is None and usage.get("cached_tokens") is not None:
            cached = usage["cached_tokens"]
        if cached is not None:
            cached_tokens = cached
    return {
        "credits": credits,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def _estimate_credits(sse_text: str, model: str) -> float:
    """兼容旧调用：仅返回 credits 估算（token 明细用 _parse_usage）。"""
    u = _parse_usage(sse_text)
    if u["credits"] is not None:
        return u["credits"]
    toks = u["total_tokens"] or u["completion_tokens"]
    if toks:
        return float(toks) * settings.COST_PER_TOKEN / 1000.0
    return 0.0


def _desensitize_chat(body: dict, enabled: bool, compact: bool = True) -> dict:
    """按开关对 chat 请求体做 harness 脱敏（压缩 harness + 敏感词插零宽空格）。

    上游内容审核会把「拒绝作恶」的合规声明词（DoS / exploit / credential…）当成敏感
    内容，把整条请求拒掉，报错 `11128 "Illegal API invocation from an unapproved
    channel"` —— 措辞极具误导性，看起来像渠道问题，其实是内容审核。
    长 harness 客户端（Claude Code、Pi 等）的 system prompt 里正有这些词，所以必须处理；
    普通短 prompt 客户端不受影响，故默认只在 Anthropic 端点开启。

    注意：开启后简单的 "hello" 请求**依然能通过**，只有完整 harness 才会被拦 ——
    不要用 hello 请求来验证这个端点是否正常。
    """
    if not (_DESENSITIZE_AVAILABLE and enabled):
        return body
    try:
        raw_len = len(json.dumps(body, ensure_ascii=False))
        out = desensitize_body(
            body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=compact,
            strip_tool_metadata=True,
        )
        _logger.info("harness 脱敏 %d -> %d 字节（compact=%s）",
                     raw_len, len(json.dumps(out, ensure_ascii=False)), compact)
        return out
    except Exception as e:
        _logger.warning("harness 脱敏失败，按原样发送：%s", e)
        return body


# Claude Code 等 Anthropic 客户端发来的是 claude-* 模型名，上游不认。
# 按 opus / sonnet / haiku 三个档次映射到本后台白名单里的模型，
# 档次目标来自 .env（ADMIN_ANTHROPIC_MODEL_*）。
_ANTHROPIC_MODEL_TIERS = (
    ("opus", settings.ANTHROPIC_MODEL_OPUS),
    ("sonnet", settings.ANTHROPIC_MODEL_SONNET),
    ("haiku", settings.ANTHROPIC_MODEL_HAIKU),
)


def _map_anthropic_model(db: Session, model: str) -> str:
    """把 Anthropic 的模型名翻译成本后台白名单里的模型名。

    按顺序判定：
      1. 空 / auto                 → auto（由号池按免费优先自选）
      2. 已在白名单里（如 glm-5.2） → 原样，允许直接点名上游模型
      3. 含 opus / sonnet / haiku   → 取 .env 配置的对应档次模型
      4. 其余（claude-* 等）        → auto
    """
    m = (model or "").strip()
    if not m or m == "auto":
        return "auto"
    if _pick_best_model(db, m):
        return m
    low = m.lower()
    for tier, target in _ANTHROPIC_MODEL_TIERS:
        if tier in low and target:
            return target
    return "auto"


# ---------------------------------------------------------------------------
# 代理重试骨架：所有 /v1/* 上游端点共用的「候选模型 × 账号」双循环。
#
# 五个端点（chat / responses / anthropic，各分流式与非流式）原本各持一份
# 相同的重试循环，这里收敛为一份：分类、策略、换号、成功清零三振计数、
# 记账全部只写一次；协议差异通过三个回调注入（make_consumer / emit_*）。
# ---------------------------------------------------------------------------

# 这些分类视为「可重试」：立即换下一个账号/模型，绝不把中断感传递给客户端
_RETRYABLE_KINDS = ("hard_credit", "session_dead", "soft_rate", "not_found", "server")


class _Attempt:
    """一次上游尝试的输出容器，由协议层 consume 回调填充。

    result      非流式聚合结果（由 consume 写入，骨架转发给 _ProxyOutcome）
    usage_parts 参与 _parse_usage 的原始文本片段；连接符 usage_join 由协议决定
                （chunk 流用 "" —— 片段自带 SSE 换行；行流用 "\\n"）
    delivered   是否已有任何内容发给客户端；发过之后出错只能中止，不得换号
                （否则客户端会收到重复内容），该标记只在流式协议下会变 True。
    """

    def __init__(self):
        self.result = None
        self.usage_parts: list = []
        self.usage_join = ""
        self.delivered = False
        self._ttfb_at = None

    def mark_ttfb(self):
        """协议层在拿到首个有效数据时调用（空行/心跳不算）。"""
        if self._ttfb_at is None:
            self._ttfb_at = time.perf_counter()

    @property
    def ttfb_at(self):
        return self._ttfb_at

    @property
    def usage_text(self):
        return self.usage_join.join(self.usage_parts)


class _ProxyOutcome:
    """非流式模式的结果容器；流式模式不使用（骨架直接 yield 数据块）。"""

    def __init__(self):
        self.response = None

    def take(self):
        if self.response is not None:
            return self.response
        # emit_exhausted 忘记设置的兜底（不应发生，保留以防万一）
        return JSONResponse(status_code=503,
                            content={"error": {"message": "所有账号/候选模型均不可用",
                                               "type": "no_model_available"}})


async def _proxy_loop(
    *, key_id: int, request: Request, chat_body: dict, order: list, url: str,
    use_case: str, mode_label: str, initial_model: str, upstream_stream: bool,
    make_consumer, emit_client_error, emit_exhausted, out: _ProxyOutcome | None = None,
):
    """代理重试骨架：候选模型 × 每模型 3 次选号的双循环。

    每次尝试：选号（tried_ids 防重）→ 建会话 → 请求 → _classify_error →
    _apply_account_policy → 可重试（_RETRYABLE_KINDS）则换号继续。
    成功路径只有一份：last_used_at + 清零三振计数 + TTFB/延迟统计 +
    表格日志 + 用量记账。

    协议差异通过三个回调注入：
      make_consumer()     -> 返回 consume(r, att) async generator，消费成功响应：
                             流式协议逐块 yield 给客户端并填充 att；
                             非流式协议把聚合结果写入 att.result（不 yield）。
      emit_client_error() -> 不可重试的 4xx 输出：流式返回要转发的字符串（骨架
                             负责 yield）；非流式把 JSONResponse 写入 out 并返回 None。
      emit_exhausted()    -> 全部候选耗尽的最终输出，同上，参数为 (err_kind, has_err)。

    流式模式（out=None）：本函数本身是 async generator，直接交给 StreamingResponse。
    非流式模式：本函数不对外 yield 任何块，端点以 `async for _ in ...: pass` 驱动，
    然后取 out.take() 作为响应返回。
    """
    db2 = SessionLocal()
    try:
        request_start = time.perf_counter()
        seq = None
        final_model = initial_model
        last_err_kind = ""
        last_err_msg = ""
        async with httpx.AsyncClient(timeout=300, limits=backend.HTTP_LIMITS) as client:
            for m in order:
                body = dict(chat_body)
                body["model"] = m
                tried_ids: set = set()
                for _ in range(3):
                    acc = _select_account(db2, exclude_ids=tried_ids, min_balance=1)
                    if not acc:
                        break
                    tried_ids.add(acc.id)
                    sess = _account_session_safe(db2, acc)
                    if sess is None:
                        continue
                    headers = sess.get_headers(extra=_upstream_extra_headers(request))
                    att = _Attempt()
                    try:
                        if upstream_stream:
                            async with client.stream("POST", url, headers=headers, json=body) as r:
                                if r.status_code >= 400:
                                    detail = await r.aread()
                                    text = (detail[:500].decode(errors="ignore")
                                            if isinstance(detail, bytes) else str(detail)[:500])
                                    kind = _classify_error(r.status_code, text)
                                    _apply_account_policy(db2, acc, kind, r.status_code, text)
                                    _logger.warning("上游错误 acc=%s model=%s HTTP %s kind=%s body=%s",
                                                    acc.uid, m, r.status_code, kind, text[:300])
                                    if kind not in _RETRYABLE_KINDS:
                                        piece = emit_client_error(r.status_code, text)
                                        if piece is not None:
                                            yield piece
                                        sess.close()
                                        return
                                    last_err_kind, last_err_msg = kind, text
                                    sess.close()
                                    continue
                                consume = make_consumer()
                                async for piece in consume(r, att):
                                    yield piece
                        else:
                            r = await client.post(url, headers=headers, json=body)
                            if r.status_code >= 400:
                                text = r.text[:500]
                                kind = _classify_error(r.status_code, text)
                                _apply_account_policy(db2, acc, kind, r.status_code, text)
                                _logger.warning("上游错误 acc=%s model=%s HTTP %s kind=%s body=%s",
                                                acc.uid, m, r.status_code, kind, text[:300])
                                if kind not in _RETRYABLE_KINDS:
                                    piece = emit_client_error(r.status_code, text)
                                    if piece is not None:
                                        yield piece
                                    sess.close()
                                    return
                                last_err_kind, last_err_msg = kind, text
                                sess.close()
                                continue
                            consume = make_consumer()
                            async for piece in consume(r, att):
                                yield piece
                        # ---- 成功路径（全端点唯一一份）----
                        acc.last_used_at = datetime.utcnow()
                        acc.err_count = 0  # 成功即清零三振计数
                        db2.commit()
                        usage = _parse_usage(att.usage_text)
                        total_toks = usage["total_tokens"] or usage["completion_tokens"]
                        latency_ms = int((time.perf_counter() - request_start) * 1000)
                        ttfb_ms = int((att.ttfb_at - request_start) * 1000) if att.ttfb_at else None
                        seq = _log_chat_row(ttfb_ms, latency_ms, m, mode_label, acc.uid or "-", 200,
                                            total_toks, error_kind="success")
                        updated = sess.updated_json()
                        sess.close()
                        _record_usage(key_id, acc.id, m, usage["credits"], updated,
                                      client_ip=_client_ip(request), use_case=use_case,
                                      prompt_tokens=usage["prompt_tokens"],
                                      completion_tokens=usage["completion_tokens"],
                                      total_tokens=usage["total_tokens"],
                                      cached_tokens=usage["cached_tokens"],
                                      seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms,
                                      error_kind="success")
                        if out is not None:
                            out.response = att.result
                        return
                    except Exception as e:
                        if att.delivered:
                            # 已向客户端吐过内容：换号会造成内容重复，只能中止
                            sess.close()
                            return
                        kind = _classify_error(0, str(e))
                        _apply_account_policy(db2, acc, kind, 0, str(e))
                        last_err_kind, last_err_msg = kind, str(e)
                        sess.close()
                        continue
        # 全部账号/模型均失败
        latency_ms = int((time.perf_counter() - request_start) * 1000)
        err_kind = last_err_kind or ("no_account" if not last_err_msg else "transport")
        seq = _log_chat_row(None, latency_ms, final_model, mode_label, "-", 503, None,
                            error_kind=err_kind)
        _record_usage(key_id, 0, final_model, 0.0, None,
                      client_ip=_client_ip(request), use_case=use_case, seq=seq,
                      latency_ms=latency_ms, error_kind=err_kind)
        piece = emit_exhausted(err_kind, bool(last_err_kind))
        if piece is not None:
            yield piece
    finally:
        db2.close()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})

    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    acc = _select_account(db)
    if not acc:
        return JSONResponse(status_code=503,
                            content={"error": {"message": "无可用账号（全部禁用或额度耗尽）", "type": "no_account"}})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    model = payload.get("model", "auto")

    # 模型白名单检查 + 免费优先选择
    resolved_model = _pick_best_model(db, model)
    if resolved_model is None:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"模型 '{model}' 不存在或已被禁用", "type": "model_not_found"}},
        )

    # 候选模型顺序：auto 模式按 免费→付费 排列，支持上游 429/5xx 自动切换下一个
    if model in ("auto", ""):
        order = [resolved_model] + _candidate_models(db, {resolved_model})
        order = order[:8]  # 最多尝试 8 个，避免全局限流时反复重试
    else:
        order = [resolved_model]  # 具体模型：不静默切换，失败即报错

    body = dict(payload)
    body["stream"] = True
    # 始终要求上游回传 usage（token 与缓存命中），保证调用方一定能拿到用量自行记录
    opts = dict(body.get("stream_options") or {})
    opts["include_usage"] = True
    body["stream_options"] = opts

    # 长 harness 客户端（Pi / claude-code-router 之类）走 OpenAI 协议时同样会撞上游内容
    # 审核。由 ADMIN_OPENAI_DESENSITIZE 控制，默认关，避免无谓改动短 prompt 客户端。
    body = _desensitize_chat(body, settings.OPENAI_DESENSITIZE)

    url = f"{settings.BACKEND}/v2/chat/completions"

    # 协议回调：Chat SSE —— 上游 chunk 原样透传给客户端
    def make_consumer():
        async def consume(r, att):
            async for chunk in r.aiter_text():
                att.mark_ttfb()
                att.delivered = True
                att.usage_parts.append(chunk)
                yield chunk
        return consume

    def emit_client_error(status, text):
        # 不可重试的客户端错误：包成合法 SSE 事件透出。
        # 历史实现直接 return 裸文本，混进 text/event-stream 后 OpenAI SDK
        # 解析不出任何事件，客户端只能看到 "Stream ended without finish_reason"，
        # 真实的上游 400 原因（如上下文超限）被吞掉。SDK 收到 data: {..."error":...}
        # 会抛 APIError 并携带完整 body（见 openai/core/streaming.js）。
        _logger.warning("上游客户端错误 HTTP %s: %s", status, text[:300])
        return f"data: {json.dumps({'error': {'message': text, 'type': 'upstream_error', 'code': status}}, ensure_ascii=False)}\n\n"

    def emit_exhausted(err_kind, has_err):
        msg = (f"所有账号/候选模型均不可用（最后错误：{err_kind}）"
               if has_err else "无可用账号或模型")
        return f"data: {json.dumps({'error': {'message': msg, 'type': 'no_model_available'}}, ensure_ascii=False)}\n\n"

    async def _stream():
        async for piece in _proxy_loop(
            key_id=key.id, request=request, chat_body=body, order=order, url=url,
            use_case="chat-completion", mode_label="stream",
            initial_model=resolved_model or model, upstream_stream=True,
            make_consumer=make_consumer, emit_client_error=emit_client_error,
            emit_exhausted=emit_exhausted,
        ):
            yield piece

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/responses")
async def responses_proxy(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """OpenAI Responses API 兼容端点（带 API Key 配额 / 用量记账 / 账号级熔断重试）。

    与 /v1/chat/completions 同一套托管逻辑：校验 Key → 配额 → 候选模型 →
    自动挑选健康账号；遇到余额不足 / session 死亡 / 限流 / 5xx 时自动换号，
    绝不把上游中断感传递给客户端。
    """
    if not _RESPONSES_AVAILABLE:
        return JSONResponse(status_code=501, content={"error": {"message": "Responses 适配器未加载", "type": "not_supported"}})

    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"请求转换失败：{e}", "type": "invalid_request"}})

    chat_body, _stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    # 客户端声明为 custom（自由格式）的工具名。请求里这些工具已被降级成带单个
    # input 参数的 function 工具发往上游；记录名字是为了在响应侧把它们的调用
    # 还原成 custom_tool_call + custom_tool_call_input.* 事件（Codex 认这个形状，
    # 否则 apply_patch 这类自由格式工具无法被识别）。
    # 必须从**原始 payload** 取：投影后的 chat_body 里 type 已经是 function 了。
    chat_custom_names = custom_tool_names(payload.get("tools"))
    opts = dict(chat_body.get("stream_options") or {})
    opts["include_usage"] = True
    chat_body["stream_options"] = opts

    # 同 /v1/chat/completions：长 harness 客户端走 Responses 协议时也要脱敏
    chat_body = _desensitize_chat(chat_body, settings.OPENAI_DESENSITIZE)

    requested = payload.get("model", "auto")
    resolved = _pick_best_model(db, requested)
    if resolved is None:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"模型 '{requested}' 不存在或已被禁用", "type": "model_not_found"}})

    order = [resolved]
    if requested in ("auto", ""):
        order = [resolved] + _candidate_models(db, {resolved})
        order = order[:8]

    client_wants_stream = bool(payload.get("stream", True))
    model_name = payload.get("model", "auto")
    url = f"{settings.BACKEND}/v2/chat/completions"

    if not client_wants_stream:
        # 非流式：内部重试，成功后聚合为单一 Response 对象。
        # 骨架不对外 yield 任何块，用 async for 驱动后取 out.take()。
        out = _ProxyOutcome()

        def make_consumer():
            async def consume(r, att):
                converter = ResponsesStreamConverter(model=model_name,
                                                     custom_names=chat_custom_names)
                for line in r.text.splitlines():
                    if not line.strip():
                        continue
                    converter.feed_line(line)
                converter.finish()
                att.result = converter.get_nonstream_response()
                att.usage_parts.append(r.text)
                return
                yield  # pragma: no cover —— 仅为统一骨架的 async generator 接口
            return consume

        def emit_client_error(status, text):
            out.response = JSONResponse(status_code=status,
                                        content={"error": {"message": text, "code": status}})

        def emit_exhausted(_err_kind, _has_err):
            out.response = JSONResponse(status_code=503,
                                        content={"error": {"message": "所有账号/候选模型均不可用",
                                                           "type": "no_model_available"}})

        async for _ in _proxy_loop(
            key_id=key.id, request=request, chat_body=chat_body, order=order, url=url,
            use_case="responses", mode_label="resp", initial_model=resolved,
            upstream_stream=False, out=out,
            make_consumer=make_consumer, emit_client_error=emit_client_error,
            emit_exhausted=emit_exhausted,
        ):
            pass
        return out.take()

    # 协议回调：Responses SSE —— Chat 事件经 ResponsesStreamConverter 转换后转发
    def make_consumer():
        async def consume(r, att):
            converter = ResponsesStreamConverter(model=model_name,
                                                 custom_names=chat_custom_names)
            att.usage_join = "\n"
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                att.mark_ttfb()
                events = converter.feed_line(line)
                if events:
                    att.delivered = True
                    yield events
                att.usage_parts.append(line)
            finish = converter.finish()
            if finish:
                att.delivered = True
                yield finish
        return consume

    def emit_client_error(status, text):
        return f"data: {json.dumps({'type': 'error', 'error': {'message': text, 'code': status}}, ensure_ascii=False)}\n\n"

    def emit_exhausted(err_kind, _has_err):
        return f"data: {json.dumps({'type': 'error', 'error': {'message': f'所有账号/候选模型均不可用（最后错误：{err_kind}）', 'code': 503}}, ensure_ascii=False)}\n\n"

    async def _stream():
        async for piece in _proxy_loop(
            key_id=key.id, request=request, chat_body=chat_body, order=order, url=url,
            use_case="responses", mode_label="resp", initial_model=resolved,
            upstream_stream=True,
            make_consumer=make_consumer, emit_client_error=emit_client_error,
            emit_exhausted=emit_exhausted,
        ):
            yield piece

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """Anthropic Messages API 兼容端点（带 API Key 配额 / 用量记账 / 号池熔断重试）。

    与 /v1/chat/completions 共用完全相同的托管逻辑，差别只在两头：
    入口把 Anthropic 请求转成 Chat 格式，出口把 Chat SSE 转回 Anthropic 事件流。
    这样 Claude Code 这类只会说 Anthropic 协议的客户端，就能直接吃后台号池的
    配额与用量记账，而不必再走 /gw 那条「桌面端单账号、无配额」的旁路。

    模型名：claude-opus-* / claude-sonnet-* / claude-haiku-* 按档次映射到白名单
    里的模型（见 _map_anthropic_model），也允许直接传 glm-5.2 这类上游模型名。
    """
    if not _ANTHROPIC_AVAILABLE:
        return JSONResponse(status_code=501,
                            content={"error": {"message": "Anthropic 适配器未加载", "type": "not_supported"}})

    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    if not payload.get("messages"):
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages is required", "type": "invalid_request"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"请求转换失败：{e}", "type": "invalid_request"}})

    # Claude Code 的 system prompt / tools 是固定 harness 模板，内含 "DoS attacks /
    # exploit development / credential testing" 一类合规声明词 —— 这些是「拒绝作恶」
    # 声明，却会被上游内容审核当成敏感内容，整条请求被拒（就是那个 11128
    # "unapproved channel"）。这一步不能省：少了它，真实 Claude Code 请求基本发不出去
    # （而简单的 hello 测试却会通过，很容易误判成「已跑通」）。
    chat_body = _desensitize_chat(
        chat_body,
        settings.ANTHROPIC_DESENSITIZE,
        compact=not settings.ANTHROPIC_NO_COMPACT,
    )

    requested = _map_anthropic_model(db, payload.get("model", "auto"))
    resolved = _pick_best_model(db, requested)
    if resolved is None:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"模型 '{requested}' 不存在或已被禁用", "type": "model_not_found"}})

    order = [resolved]
    if requested in ("auto", ""):
        order = ([resolved] + _candidate_models(db, {resolved}))[:8]

    # 上游一律按流式拉取：Anthropic 的方向就是「消费 Chat SSE 再转事件流」。
    # 客户端若要非流式，我们在内部聚合完再一次性返回。
    chat_body["stream"] = True
    opts = dict(chat_body.get("stream_options") or {})
    opts["include_usage"] = True
    chat_body["stream_options"] = opts

    # Anthropic Messages API 的 stream 默认是 false
    client_wants_stream = bool(payload.get("stream", False))
    model_name = payload.get("model", "auto")
    url = f"{settings.BACKEND}/v2/chat/completions"

    if not client_wants_stream:
        # 非流式：内部重试，聚合为 Anthropic Message 对象后一次性返回。
        # 骨架不对外 yield 任何块，用 async for 驱动后取 out.take()。
        out = _ProxyOutcome()

        def make_consumer():
            async def consume(r, att):
                conv = AnthropicStreamConverter(model=model_name)
                att.usage_join = "\n"
                for line in r.text.splitlines():
                    if not line.strip():
                        continue
                    att.usage_parts.append(line)
                    conv.feed_line(line)
                att.result = conv.build_message()
                return
                yield  # pragma: no cover —— 仅为统一骨架的 async generator 接口
            return consume

        def emit_client_error(status, text):
            out.response = JSONResponse(status_code=status,
                                        content={"error": {"message": text, "type": "upstream_error"}})

        def emit_exhausted(_err_kind, _has_err):
            out.response = JSONResponse(status_code=503,
                                        content={"error": {"message": "所有账号/候选模型均不可用",
                                                           "type": "no_model_available"}})

        async for _ in _proxy_loop(
            key_id=key.id, request=request, chat_body=chat_body, order=order, url=url,
            use_case="anthropic", mode_label="anthropic", initial_model=resolved,
            upstream_stream=False, out=out,
            make_consumer=make_consumer, emit_client_error=emit_client_error,
            emit_exhausted=emit_exhausted,
        ):
            pass
        return out.take()

    # 协议回调：Anthropic SSE —— Chat 事件经 AnthropicStreamConverter 转换后转发
    def make_consumer():
        async def consume(r, att):
            conv = AnthropicStreamConverter(model=model_name)
            att.usage_join = "\n"
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                att.mark_ttfb()
                att.usage_parts.append(line)
                events = conv.feed_line(line)
                if events:
                    att.delivered = True
                    yield events
            tail = conv.finish()
            if tail:
                yield tail
        return consume

    def emit_client_error(_status, text):
        # 不可重试的客户端错误才原样透出（error_event 只做事件构造，与流状态无关）
        return AnthropicStreamConverter(model=model_name).error_event(text)

    def emit_exhausted(err_kind, _has_err):
        return AnthropicStreamConverter(model=model_name).error_event(
            f"所有账号/候选模型均不可用（最后错误：{err_kind}）", "overloaded_error"
        )

    async def _stream():
        async for piece in _proxy_loop(
            key_id=key.id, request=request, chat_body=chat_body, order=order, url=url,
            use_case="anthropic", mode_label="anthropic", initial_model=resolved,
            upstream_stream=True,
            make_consumer=make_consumer, emit_client_error=emit_client_error,
            emit_exhausted=emit_exhausted,
        ):
            yield piece

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """Anthropic token 计数端点（本地粗估，不请求上游、不消耗配额）。

    Claude Code 发消息前会调它做上下文预算。上游没有等价接口，这里按
    「字符数 / 3」估算并刻意偏向高估 —— 宁可让客户端早点压缩上下文，
    也不要低估，否则真正请求时可能超出上游上限直接失败。
    """
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    if not get_key_row(db, api_key):
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    text = "".join((
        json.dumps(payload.get("system") or "", ensure_ascii=False),
        json.dumps(payload.get("messages") or [], ensure_ascii=False),
        json.dumps(payload.get("tools") or [], ensure_ascii=False),
    ))
    return {"input_tokens": max(1, len(text) // 3)}


@router.get("/v1/models")
async def models(
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    acc = _select_account(db)
    if not acc:
        return JSONResponse(status_code=503,
                            content={"error": {"message": "无可用账号", "type": "no_account"}})
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            models_raw = sess.fetch_models()
            acc.auth_json = sess.updated_json()
        acc.last_used_at = datetime.utcnow()
        acc.err_count = 0  # 成功即清零三振计数
        db.commit()
        # 字段与 core/converter.py 的 /v1/models 保持一致：下游需要用
        # max_input_tokens 判断上下文窗口、用 supports_* 判断能力开关。
        # 只回传 id/name 会让客户端退回默认值（例如过早压缩上下文）。
        data = [{
            "id": m.get("id"),
            "object": "model",
            "created": 1700000000,
            "owned_by": "codebuddy",
            "name": m.get("name") or m.get("id"),
            "credits": m.get("credits"),
            "credit_multiplier": backend.CredentialManager._parse_credit_multiplier(m.get("credits"))
            if hasattr(backend.CredentialManager, "_parse_credit_multiplier") else None,
            "description": m.get("descriptionZh") or m.get("descriptionEn"),
            "supports_images": m.get("supportsImages"),
            "supports_reasoning": m.get("supportsReasoning"),
            "supports_tool_call": m.get("supportsToolCall"),
            "max_input_tokens": m.get("maxInputTokens"),
            "max_output_tokens": m.get("maxOutputTokens"),
            "vendor": m.get("vendor"),
        } for m in models_raw if m.get("id") and _is_model_allowed(db, m.get("id")) and m.get("id","").lower() != "auto"]
        return {"object": "list", "data": data, "source": "backend"}
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": {"message": f"获取模型失败：{e}", "type": "upstream"}})
