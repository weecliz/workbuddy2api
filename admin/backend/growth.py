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
    # pi-lens-ignore: unchecked-throwing-call-python
    return int(data.get("reward_credit") or 0)


def growth_streak_days(cm) -> int:
    """连登天数（data.streak.days）；缺字段返回 0（活跃上报被静默丢弃的告警信号）。"""
    streak = _growth(cm, "GET", "/streak").get("streak") or {}
    # pi-lens-ignore: unchecked-throwing-call-python
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
    # pi-lens-ignore: unchecked-throwing-call-python
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


# ---------------------------------------------------------------------------
# 成长任务中心（契约对齐 workbuddy2api-hub wb_tasks.py 实测口径，勿凭猜测增删）
# ---------------------------------------------------------------------------

def fetch_tasks(cm) -> list[dict]:
    """成长任务列表：GET /v2/activity/growth/tasks，返回归一化后的任务数组。

    每项：{task_code, name, status, current, target, reward_credit}。
    status 取 accept_status 原值：not_accepted / accepted / completed / claimed。
    上游字段缺失时回退本地规格默认值（见 admin/tasks/event_specs.py）。
    """
    resp = cm._request_backend("GET", "/v2/activity/growth/tasks")
    raw = (resp.get("data") or {}).get("tasks") or []
    # 延迟导入避免循环依赖：event_specs 是纯数据模块，growth 属契约层。
    from admin.tasks.event_specs import TASK_SPECS

    tasks: list[dict] = []
    for t in raw:
        code = t.get("task_code") or ""
        if not code:
            continue
        spec = TASK_SPECS.get(code) or {}
        prog = t.get("progress") or {}
        tasks.append({
            "task_code": code,
            "name": t.get("title") or spec.get("name") or code,
            "status": t.get("accept_status") or "not_accepted",
            # pi-lens-ignore: unchecked-throwing-call-python
            "current": int(prog.get("current") or 0),
            # pi-lens-ignore: unchecked-throwing-call-python
            "target": int(prog.get("target") or spec.get("target") or 1),
            # pi-lens-ignore: unchecked-throwing-call-python
            "reward_credit": int(t.get("reward_credit") or spec.get("reward") or 0),
        })
    return tasks


def accept_tasks(cm, codes: list[str]) -> None:
    """批量接取任务：POST /v2/activity/growth/tasks/accept。

    codes 为空时不发请求（上游对空数组的行为未知，避免无谓调用）。
    """
    if not codes:
        return
    cm._request_backend("POST", "/v2/activity/growth/tasks/accept",
                        {"task_codes": codes})


def report_events(cm, events: list[dict]) -> None:
    """批量事件上报：POST /v2/report，body 为数组（即使单条也要包成数组）。

    与 report_chat_activity 同一端点；code!=0 时 _request_backend 抛异常，
    由调用方按账号隔离处理。
    """
    if not events:
        return
    cm._request_backend("POST", "/v2/report", events)


def claim_task(cm, code: str) -> dict:
    """领取任务奖励：POST /activity/growth/tasks/{code}/claim（注意无 /v2 前缀）。

    端点前缀差异是 hub 实测口径（列表/接取带 /v2，领奖不带），不统一、不猜测。
    领奖业务码语义未全知 → 用 soft 请求拿原始 dict：code==0 返回
    {ok: True, credit, energy}；其余一律 {ok: False, msg}，不当异常抛。
    """
    resp = cm._request_backend_soft("POST", f"/activity/growth/tasks/{code}/claim", {})
    if resp.get("code") == 0:
        data = resp.get("data") or {}
        return {"ok": True,
                # pi-lens-ignore: unchecked-throwing-call-python
                "credit": int(data.get("credit") or 0),
                # pi-lens-ignore: unchecked-throwing-call-python
                "energy": int(data.get("energy") or 0)}
    return {"ok": False, "msg": str(resp.get("msg") or resp.get("code"))}
