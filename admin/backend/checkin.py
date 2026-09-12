"""每日签到领取 100 积分（Buddy 加油站活动）。

契约要点：
  - 状态查询与领取都走 _request_backend_soft：业务码非 0 不算异常（已领 / 无资格 /
    活动结束都是正常业务结果），由调用方按 status 分支处理。
  - 1003 event_ended 由调度器转成「停止领取时间」，避免活动下线后继续请求触发风控。

函数一律接收 CredentialManager 实例（cm）而非 AccountSession：
按域拆分后仍复用同一会话的 token 刷新语义，也便于单测直接喂假 cm。
"""


def _map_checkin_status(code: int) -> str:
    """后端签到业务码 → 语义状态。"""
    return {
        1001: "already_claimed",   # 今日已领取
        1002: "not_eligible",      # 无领取资格
        1003: "event_ended",       # 活动已结束
    }.get(code, "unknown")


def get_checkin_status(cm) -> dict:
    """查询当前账号的签到活动状态。

    返回后端 data 字段（含 active / today_checked_in / end_time / activity_name 等）。
    end_time 即活动结束时间，是「下次停止领取」配置的依据。
    """
    data = cm._request_backend_soft("POST", "/v2/billing/meter/checkin-activity-status", {})
    return data.get("data") or {}


def claim_daily_checkin(cm) -> dict:
    """执行每日签到领取。

    成功返回 {"ok": True, "credit": int, "streak_days": int}；
    业务失败（已领/无资格/活动结束）返回 {"ok": False, "code": int, "status": str}。
    """
    data = cm._request_backend_soft("POST", "/v2/billing/meter/daily-checkin", {})
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
