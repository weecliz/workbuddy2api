"""成长中心：猫猫领养 / 旅行 / 连登天数 / 对话活跃上报。

请求契约对齐 Go 版实现（Sliverkiss/workbuddy2api internal/upstream/travel.go、
report.go）与 88lin/workbuddy-auto-signin 实测口径；字段勿凭猜测增删。

函数一律接收 CredentialManager 实例（cm），由 AccountSession 转发。
"""
import time

from .http import _GROWTH_BASE


def _growth(cm, method: str, path: str, body: dict | None = None) -> dict:
    """growth 域请求，返回 data 字段；业务失败由 _request_backend 抛异常。"""
    resp = cm._request_backend(method, _GROWTH_BASE + path, body)
    return resp.get("data") or {}


def buddy_info(cm) -> dict | None:
    """当前猫档案；None = 无猫（data.buddy 为 null / 缺失 / 空对象）。"""
    b = _growth(cm, "GET", "/buddy/info").get("buddy")
    return b or None


def buddy_agreement(cm) -> None:
    """同意活动协议（幂等，重复调用无副作用）。"""
    _growth(cm, "POST", "/buddy/agreement", {"agree": True})


def buddy_first(cm) -> dict:
    """领养第一只猫（送 300 积分）。

    对话量门槛未达标时上游返回 HTTP 400（消息含
    "first_buddy task not completed yet"），_request_backend 抛 RuntimeError，
    由调用方识别后做当日防抖（当日不再重试）。
    """
    return _growth(cm, "POST", "/buddy/first", {})


def travel_status(cm) -> dict:
    """旅行状态：state(idle/traveling/arrived) / daily_limit_reached / record_id / reward_credit。

    daily_limit_reached = 今日已派出（上游自然日 00:00 CST 重置）。
    """
    return _growth(cm, "GET", "/buddy/travel/status")


def travel_depart(cm, location_id: int = 4) -> None:
    """派出猫旅行。location_id 1~4 实测收益/时长区间完全相同（Go 版固定 4）。"""
    _growth(cm, "POST", "/buddy/travel/depart", {"location_id": location_id})


def travel_claim(cm, record_id: int) -> int:
    """领取到站奖励（必须带 record_id），返回 reward_credit；缺失按 0 记，不算失败。"""
    data = _growth(cm, "POST", "/buddy/travel/claim", {"record_id": record_id})
    return int(data.get("reward_credit") or 0)


def growth_streak_days(cm) -> int:
    """连登天数（data.streak.days）；缺字段返回 0（活跃上报被静默丢弃的告警信号）。"""
    streak = _growth(cm, "GET", "/streak").get("streak") or {}
    return int(streak.get("days") or 0)


def report_chat_activity(cm, conversation_id: str, request_id: str = "") -> None:
    """POST /v2/report 上报一条 chat_request_send 活跃事件（body 为数组）。

    - userId 必填（= 账号 uid）：缺失时上游返回 200 但**静默丢弃**
      （progress 不动、streak 不计）。
    - conversationId 无需真实会话，服务端不校验一致性；requestId 各条独立。
    - 字段形状照抄官方客户端全量字段，勿裁剪成最小集（防上游后续加严）。
    - 一条上报同时点亮 growth 连登 + 解锁 first_buddy 领养任务。
    """
    acct = (cm._session().get("account") or {})
    now_ms = int(time.time() * 1000)
    ev = {
        "eventCode": "chat_request_send",
        "timestamp": now_ms,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conversation_id,
        "requestId": request_id or conversation_id,
        "inputLength": 12,
        "requestModelId": "deepseek-v4-flash",
        "requestModelName": "DeepSeek V4 Flash",
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now_ms,
        "traceId": "",
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "agentType": "conversation",
        "userId": acct.get("uid", ""),
    }
    cm._request_backend("POST", "/v2/report", [ev])
