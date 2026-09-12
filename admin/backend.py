"""复用 converter.CredentialManager 操作单个账号的后端会话。

账号凭据以 .info 原文形式存于 MySQL；用时落盘成临时文件交给 CredentialManager，
用完读回（token 可能被刷新），写回 MySQL。
"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from converter import CredentialManager  # 复用既有后端鉴权 / 刷新 / 模型 / 额度逻辑

# 连接池：减少 TLS 握手，与 Go 项目 MaxIdleConnsPerHost=20 对齐。
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def parse_auth_meta(auth_json: str) -> dict:
    """从 .info 原文里抽取账号元信息（uid / enterpriseId / domain / 昵称）。"""
    try:
        data = json.loads(auth_json)
    except Exception:
        return {}
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    return {
        "uid": str(acct.get("uid") or ""),
        "enterprise_id": str(acct.get("enterpriseId") or ""),
        "domain": str(auth.get("domain") or ""),
        "nickname": str(acct.get("nickname") or ""),
    }


class AccountSession:
    """把一个账号的 auth_json 包成可用的后端会话。

    出站身份（UA / X-Device-Token 策略）由凭据的 auth.domain 在
    CredentialManager 内自动推断，无需外部指定。
    """

    def __init__(self, auth_json: str):
        # 用 mkstemp 而不是 mktemp：后者在「取名字」与「写入」之间有 TOCTOU 窗口。
        # mkstemp 会返回已打开的 fd，这里立刻关掉（路径已经安全创建好了）。
        _fd, self._path = tempfile.mkstemp(suffix=".info")
        os.close(_fd)
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(auth_json)
        self.cm = CredentialManager(Path(self._path))

    def get_headers(self, extra: dict | None = None) -> dict:
        return self.cm.get_headers(extra=extra)

    def fetch_models(self) -> list:
        return self.cm.fetch_models()

    def fetch_balance(self) -> dict:
        return self.cm.fetch_balance()

    def fetch_credit_details(self) -> list[dict]:
        """获取积分明细（每个积分包的总量/剩余/到期时间）。

        对应截图中的「版本基础用量」「权益赠送包」等条目。
        剩余额度使用 CycleCapacityRemain（当前周期剩余），与官方界面「累积剩余」对齐；
        CapacityRemain 仅作为账号层级总剩余保留在字段 account_remain 中供参考。
        """
        data = self.cm._request_backend("POST", "/v2/billing/meter/get-user-resource", {})
        resp = data.get("data", {}).get("Response", {}).get("Data", {}) or {}
        packages = []
        for a in resp.get("Accounts") or []:
            if a.get("CapacityUnit") != "credits":
                continue
            # CycleEndTime = 当前周期结束时间（如 "2026-09-30 23:59:59"）
            # DeductionEndTime = 绝对到期时间戳（毫秒），0 表示永不过期
            # ExpiredTime = 已过期时间（通常为空串，表示未过期）
            cycle_end = a.get("CycleEndTime") or ""
            deduction_end_ts = a.get("DeductionEndTime") or 0
            packages.append({
                "name": a.get("PackageName") or a.get("Name") or "未命名",
                "total": a.get("CapacitySize") or 0,
                # 真实可用额度以当前周期剩余为准（体验版用完时 CapacityRemain 仍可能为 500）
                "remain": a.get("CycleCapacityRemain") or 0,
                "used": a.get("CycleCapacityUsed") or 0,
                "account_remain": a.get("CapacityRemain") or 0,
                "account_used": a.get("CapacityUsed") or 0,
                "cycle_start": a.get("CycleStartTime") or "",
                "cycle_end": cycle_end,
                "deduction_end_ts": deduction_end_ts,
                "deduction_end": datetime.fromtimestamp(deduction_end_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(deduction_end_ts, (int, float)) and deduction_end_ts > 0 else "",
                "status": a.get("Status"),
                "package_code": a.get("PackageCode") or "",
            })
        return packages

    def fetch_request_usage(self, start_time: str, end_time: str, page_num: int = 1, page_size: int = 10) -> dict:
        """获取模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
        return self.cm._request_backend("POST", "/billing/meter/get-user-request-usage", {
            "startTime": start_time,
            "endTime": end_time,
            "pageNum": page_num,
            "pageSize": page_size,
        })

    # -----------------------------------------------------------------------
    # 每日签到领取 100 积分（Buddy 加油站活动）
    # -----------------------------------------------------------------------

    def get_checkin_status(self) -> dict:
        """查询当前账号的签到活动状态。

        返回后端 data 字段（含 active / today_checked_in / end_time / activity_name 等）。
        end_time 即活动结束时间，是「下次停止领取」配置的依据。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/checkin-activity-status", {})
        return data.get("data") or {}

    def claim_daily_checkin(self) -> dict:
        """执行每日签到领取。

        成功返回 {"ok": True, "credit": int, "streak_days": int}；
        业务失败（已领/无资格/活动结束）返回 {"ok": False, "code": int, "status": str}。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/daily-checkin", {})
        code = data.get("code")
        payload = data.get("data") or {}
        if code and code != 0:
            return {"ok": False, "code": code, "status": _map_checkin_status(code), "msg": data.get("msg")}
        credit = payload.get("credit")
        if credit is None:
            credit = data.get("credit")
        streak = payload.get("streak_days")
        if streak is None:
            streak = data.get("streak_days")
        return {"ok": True, "credit": credit or 0, "streak_days": streak or 0}

    def get_token_expiry(self) -> int:
        """返回 token 到期时间戳（毫秒），0 表示未知。"""
        auth = self.cm._auth or {}
        return auth.get("expiresAt") or 0

    def updated_json(self) -> str:
        with open(self._path, "r", encoding="utf-8") as f:
            return f.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass


def _map_checkin_status(code: int) -> str:
    """后端签到业务码 → 语义状态。"""
    return {
        1001: "already_claimed",   # 今日已领取
        1002: "not_eligible",      # 无领取资格
        1003: "event_ended",       # 活动已结束
    }.get(code, "unknown")
