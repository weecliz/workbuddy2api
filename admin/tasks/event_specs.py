"""成长任务的事件规格与构造器（纯数据 + 纯函数，不依赖会话与数据库）。

契约对齐 workbuddy2api-hub 的 wb_tasks.py 实测口径：
  - TASK_SPECS     覆盖国内版成长中心全部任务（code → kind/target/reward/name）
  - SKIP_CODES     不可/不宜自动完成的任务，编排层直接跳过
  - build_event()  按任务 kind 构造上报事件体

字段原则：照抄 hub 的全量字段，勿裁剪成最小集（防上游后续加严校验）；
userId 必填（= 账号 uid，缺失时上游 200 但静默丢弃）；conversationId /
requestId 无需真实会话，服务端不校验一致性。
"""
import time


# ---------------------------------------------------------------------------
# 任务规格表（code → 规格）
# ---------------------------------------------------------------------------
# kind 决定 build_event 的事件模板；target 是点亮所需次数；
# reward 是参考奖励积分（真实值以上游任务列表 reward_credit 为准）。
TASK_SPECS: dict[str, dict] = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "Model_chat_GLM5.2": {"kind": "glmchat", "target": 1, "reward": 100, "name": "体验 GLM-5.2"},
    "black_cat": {"kind": "cat", "target": 3, "reward": 100, "name": "夜猫子任务 (23:00-08:00)"},
    # ---- 以下任务 hub 无对应事件分支（落 heartbeat 点不亮）或归属其它模块，统一跳过 ----
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Buddy_App_QQ": {"kind": "buddy5", "target": 1, "reward": 100, "name": "企鹅教师助手"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    "first_buddy": {"kind": "buddy_first", "target": 1, "reward": 0, "name": "领养首只猫猫"},
    # 真实捐款动作，不可伪造（hub 亦跳过）
    "Expert_Philanthropy": {"kind": "unforgeable", "target": 1, "reward": 0, "name": "公益爱心捐赠"},
}

# 编排层跳过的任务：
#   - 事件分支缺失（buddy5 / richmeow / library），上报 heartbeat 点不亮白白发
#   - first_buddy（领养）由 cat_travel 任务的状态机处理，这里不碰
#   - unforgeable（真实捐款）不可伪造
SKIP_KINDS = {"buddy5", "richmeow", "library", "buddy_first", "unforgeable"}


def is_skipped(code: str) -> bool:
    """该任务是否应被编排层跳过（规格缺失也视为跳过——未知任务不盲报）。"""
    spec = TASK_SPECS.get(code)
    return spec is None or spec["kind"] in SKIP_KINDS


# 夜猫子任务的点亮时段（上游自然日口径，CST 23:00-08:00 之外上报无效）
NIGHT_KINDS = {"cat"}


def in_night_window(now_utc=None) -> bool:
    """当前是否处于夜猫子点亮窗口（CST 23:00-08:00）。

    now_utc 参数仅供测试注入；默认取当前 UTC 时间。
    """
    from datetime import datetime, timedelta

    now = now_utc or datetime.utcnow()
    cst = now + timedelta(hours=8)
    return cst.hour >= 23 or cst.hour < 8


def build_event(uid: str, kind: str, idx: int = 0) -> dict:
    """按任务 kind 构造一条上报事件（从 hub wb_tasks.build_event 移植）。

    uid 为账号 uid（userId 必填）；idx 用于同任务多条上报间的 id/会话区分。
    未知 kind 返回 heartbeat 事件（上游忽略，不会报错）。
    """
    # pi-lens-ignore: unchecked-throwing-call-python, ast-grep:unchecked-throwing-call-python
    now = int(time.time() * 1000)
    cid = f"wb2api-task-{now}-{idx}"
    rid = f"{cid}-req"

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        ex_id = "ex_2cvvUZQhDyeJ" if kind == "lighthouse" else ("CloudOpsTeam" if kind == "team" else "ContentCreator")
        name = "腾讯轻量云专家" if kind == "lighthouse" else ("运维专家团队" if kind == "team" else "内容创作专家")
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "playbook":
        return {"eventCode": "playbook_prompt_send", "timestamp": now, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "other", "promptLength": 10, "isOfficial": 1,
                "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "glmchat", "cat"):
        m_id = "glm-5.2" if kind in ("glmchat", "cat") else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind in ("glmchat", "cat") else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}
