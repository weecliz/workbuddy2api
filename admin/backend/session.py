"""单个账号的上游会话：凭据落盘 → CredentialManager → token 读回 → 删临时文件。

账号凭据以 .info 原文形式存于数据库；用时落盘成临时文件交给 CredentialManager
（复用桌面端那套鉴权 / 刷新 / 请求逻辑），用完读回（token 可能被刷新）写回数据库。

本模块只放「会话生命周期 + 档案与额度」；签到、成长中心的实现分别在
checkin.py / growth.py，这里做薄转发，对外仍是同一个 AccountSession 类。
"""
import os
import tempfile
from datetime import datetime
from pathlib import Path

from core.converter import CredentialManager  # 复用既有后端鉴权 / 刷新 / 模型 / 额度逻辑

from . import checkin as _checkin
from . import growth as _growth


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
        # pi-lens-ignore: unchecked-throwing-call-python
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(auth_json)
        self.cm = CredentialManager(Path(self._path))

    # -----------------------------------------------------------------------
    # 会话基础
    # -----------------------------------------------------------------------

    def get_headers(self, extra: dict | None = None) -> dict:
        return self.cm.get_headers(extra=extra)

    def get_token_expiry(self) -> int:
        """返回 token 到期时间戳（毫秒），0 表示未知。

        用 CredentialManager 的公开 summary() 取 token_expires_at，而不是直接
        摸私有属性 —— 这里曾写作 `self.cm._auth`，但 CredentialManager 上
        根本没有 _auth（真正存会话的是 _cached），一调用就抛 AttributeError。
        summary() 内部会先 _load_if_stale()，因此外部刷新过文件也能读到新值。
        """
        # pi-lens-ignore: unchecked-throwing-call-python
        return int(self.cm.summary().get("token_expires_at") or 0)

    def updated_json(self) -> str:
        # pi-lens-ignore: unchecked-throwing-call-python
        with open(self._path, "r", encoding="utf-8") as f:
            return f.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass

    # -----------------------------------------------------------------------
    # 档案与额度
    # -----------------------------------------------------------------------

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
    # 每日签到（实现见 checkin.py）
    # -----------------------------------------------------------------------

    def get_checkin_status(self) -> dict:
        return _checkin.get_checkin_status(self.cm)

    def claim_daily_checkin(self) -> dict:
        return _checkin.claim_daily_checkin(self.cm)

    # -----------------------------------------------------------------------
    # 成长中心（实现见 growth.py）
    # -----------------------------------------------------------------------

    def buddy_info(self) -> dict | None:
        return _growth.buddy_info(self.cm)

    def buddy_agreement(self) -> None:
        return _growth.buddy_agreement(self.cm)

    def buddy_first(self) -> dict:
        return _growth.buddy_first(self.cm)

    def travel_status(self) -> dict:
        return _growth.travel_status(self.cm)

    def travel_depart(self, location_id: int = 4) -> None:
        return _growth.travel_depart(self.cm, location_id)

    def travel_claim(self, record_id: int) -> int:
        return _growth.travel_claim(self.cm, record_id)

    def growth_streak_days(self) -> int:
        return _growth.growth_streak_days(self.cm)

    def report_chat_activity(self, conversation_id: str, request_id: str = "") -> None:
        return _growth.report_chat_activity(self.cm, conversation_id, request_id)

    # -----------------------------------------------------------------------
    # 成长任务中心（实现见 growth.py，契约对齐 hub wb_tasks.py）
    # -----------------------------------------------------------------------

    def fetch_tasks(self) -> list[dict]:
        return _growth.fetch_tasks(self.cm)

    def accept_tasks(self, codes: list[str]) -> None:
        return _growth.accept_tasks(self.cm, codes)

    def report_events(self, events: list[dict]) -> None:
        return _growth.report_events(self.cm, events)

    def claim_task(self, code: str) -> dict:
        return _growth.claim_task(self.cm, code)
