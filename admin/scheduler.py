"""轻量后台定时任务调度器（无第三方依赖）。

用守护线程每 15s 轮询 schedules 表，到点（next_run_at <= now）的任务就执行，
执行完更新 last_run_at / next_run_at / last_result。

支持的任务：
  - refresh_balances：遍历 active 账号刷新余额（统计平台总积分）
  - sync_models：从后端拉取最新模型列表并 upsert 倍率
  - daily_checkin：每日签到领取积分
  - cat_travel：猫猫旅行巡检（领养 / 派出 / 领奖状态机）
  - activity_report：对话活跃上报（点亮连登 + 解锁领猫任务）
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

from admin.db import SessionLocal
from admin.models import Schedule

# ---------------------------------------------------------------------------
# 猫猫旅行 / 活跃上报的共享风控参数（对齐 Go 版 scheduler 口径）
# ---------------------------------------------------------------------------

_TRAVEL_LOCATION_ID = 4     # 派出地点固定 4：1~4 收益/时长区间完全相同，无最优解
_ACCOUNT_DELAY = 0.8        # 账号间限速（秒）：全量账号约 0.8s/个，避免上游风控
_REPORT_GAP = 1.5           # 同一账号连续上报之间的间隔（秒）：秒发易触发风控
_ADOPT_THRESHOLD_MARKER = "first_buddy task not completed yet"

# 领养门槛未达的当日防抖：uid → 上游自然日（CST）。同日不再重试领养，
# 避免对上游重试轰炸；进程重启即清零（与 Go 版一致，无需持久化）。
_adopt_tried: dict[str, str] = {}


def _upstream_today() -> str:
    """上游自然日（00:00 CST 重置）格式 YYYY-MM-DD。"""
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def _adopt_tried_today(uid: str) -> bool:
    return _adopt_tried.get(uid) == _upstream_today()


def _mark_adopt_tried(uid: str) -> None:
    _adopt_tried[uid] = _upstream_today()


def _is_adopt_threshold_error(e: Exception) -> bool:
    """领养对话量门槛未达标：上游 HTTP 400 + 固定关键词，属预期行为。"""
    return _ADOPT_THRESHOLD_MARKER in str(e).lower()


def _fmt_travel_eta(st: dict) -> str:
    """从 travel/status 原始响应估算回程时间（arrive_at / server_now 毫秒时间戳口径）。

    字段缺失或类型不符时返回空串——不猜格式，宁可不显示（与 88lin 实测脚本对齐，
    server_now 比本地时钟可靠）。
    """
    try:
        arrive = st.get("arrive_at")
        now = st.get("server_now")
        if not isinstance(arrive, (int, float)) or not arrive:
            return ""
        server = now if isinstance(now, (int, float)) and now else int(time.time() * 1000)
        hours = (arrive - server) / 3600000.0
        if hours <= 0:
            return ",即将到站"
        if hours >= 1:
            return f",约{hours:.0f}小时后回"
        return f",约{int(hours * 60)}分钟后回"
    except Exception:
        return ""


def _activity_report_count() -> int:
    """每号每次活跃上报的条数（env ADMIN_ACTIVITY_REPORT_COUNT，默认 5）。

    领养猫（buddy/first）前置需 5 次对话（chat_5 任务），默认 5 条同一
    conversationId 内多轮上报刚好刷满门槛；配置 <=0 时回落 1。
    """
    try:
        n = int(os.getenv("ADMIN_ACTIVITY_REPORT_COUNT", "5"))
    except (TypeError, ValueError):
        n = 5
    return n if n > 0 else 1


def run_task(task: str, db, schedule: "Schedule | None" = None) -> dict:
    """执行某个任务，返回结果摘要字典。"""
    if task == "refresh_balances":
        from admin.routers import accounts as acc_router
        from admin.models import Account
        ok = fail = 0
        for a in db.query(Account).filter(Account.status == "active").all():
            if acc_router._refresh_balance(a):
                ok += 1
            else:
                fail += 1
            db.commit()
        return {"task": task, "refreshed": ok, "failed": fail}
    if task == "sync_models":
        from admin.routers import models as models_router
        return models_router._do_sync_models(db)
    if task == "daily_checkin":
        return run_daily_checkin(db, schedule)
    if task == "cat_travel":
        return run_cat_travel(db, schedule)
    if task == "activity_report":
        return run_activity_report(db, schedule)
    return {"task": task, "error": "未知任务类型"}


def run_daily_checkin(db, schedule: "Schedule | None" = None) -> dict:
    """遍历活跃账号执行每日签到领取 100 积分。

    风控要点：
      - 全部请求经 CredentialManager 注入 X-Device-Token（与桌面端一致）。
      - 若任务配置了 stop_after（下次停止领取时间），到达后直接跳过，不再发领取请求，
        避免活动下线后继续请求触发上游风控。
      - 若某账号领取返回 EventEnded(1003)，自动把 stop_after 设为今天，后续不再尝试。
    """
    from admin.models import Account
    from admin.backend import AccountSession

    now = datetime.utcnow()

    # 停止领取时间：到达则跳过
    if schedule is not None and schedule.stop_after is not None:
        if now > schedule.stop_after:
            return {"task": "daily_checkin", "skipped": "已超过停止领取时间，不再请求",
                    "stop_after": schedule.stop_after.isoformat()}

    claimed = skipped_already = failed = 0
    ended = False
    errors: list[str] = []
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            with AccountSession(a.auth_json) as sess:
                st = sess.get_checkin_status()
                if st.get("today_checked_in"):
                    skipped_already += 1
                else:
                    res = sess.claim_daily_checkin()
                    if res.get("ok"):
                        claimed += 1
                    elif res.get("status") == "event_ended":
                        ended = True
                        failed += 1
                        errors.append(f"acc{a.id}:活动已结束")
                    else:
                        failed += 1
                        errors.append(f"acc{a.id}:{res.get('status') or res.get('msg')}")
                # 写回可能已刷新的 token（签到请求会触发鉴权头刷新）
                a.auth_json = sess.updated_json()
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")

    # 发现活动已结束：自动把停止时间设为今天，防止后续继续请求
    if ended and schedule is not None:
        schedule.stop_after = now
        db.commit()

    return {
        "task": "daily_checkin",
        "claimed": claimed,
        "skipped_already": skipped_already,
        "failed": failed,
        "activity_ended": ended,
        "errors": errors[:10],
    }


def run_cat_travel(db, schedule: "Schedule | None" = None) -> dict:
    """猫猫旅行巡检：每个活跃账号单趟推进一个动作（对齐 Go 版 travelOne 状态机）。

    无猫 → 同意协议 + 尝试领养（+300 积分；对话量门槛未达时上游 400，
           当日不再重试，避免对上游重试轰炸）。
    有猫 → 按 travel/status 分派：
           arrived   → claim 领奖（必须带 record_id）
           idle      → 未达当日名额时 depart 派出（location_id 固定 4）
           traveling → 跳过（在途）

    风控要点：账号间 sleep 0.8s；单号单动作不轮询不等待；查询失败只跳过该号。
    """
    from admin.models import Account
    from admin import backend as be

    departed = claimed = adopted = skipped = failed = 0
    errors: list[str] = []
    details: list[str] = []
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            with be.AccountSession(a.auth_json) as sess:
                try:
                    buddy = sess.buddy_info()
                    if buddy is None:
                        if _adopt_tried_today(a.uid or ""):
                            skipped += 1  # 当日已判定门槛未达，不再重试
                            details.append(f"acc{a.id}:防抖跳过(当日领养门槛未达,先跑活跃上报刷对话量)")
                        else:
                            sess.buddy_agreement()  # 幂等
                            try:
                                sess.buddy_first()
                                adopted += 1
                                details.append(f"acc{a.id}:领养成功(+300)")
                            except Exception as e:
                                if _is_adopt_threshold_error(e):
                                    _mark_adopt_tried(a.uid or "")
                                    skipped += 1
                                    details.append(f"acc{a.id}:领养门槛未达(需对话量,先跑活跃上报)")
                                else:
                                    raise
                    else:
                        st = sess.travel_status()
                        state = st.get("state")
                        name = (buddy.get("name") if isinstance(buddy, dict) else None) or "猫"
                        if state == "arrived":
                            rid = int(st.get("record_id") or 0)
                            if rid:
                                reward = sess.travel_claim(rid)
                                claimed += 1
                                details.append(f"acc{a.id}:领奖+{reward}({name})")
                            else:
                                skipped += 1  # arrived 但无 record_id，无法领奖
                                details.append(f"acc{a.id}:到站但缺record_id,无法领奖")
                        elif state == "idle":
                            if st.get("daily_limit_reached"):
                                skipped += 1  # 服务端明确名额已用完，不白撞
                                details.append(f"acc{a.id}:今日名额已用完({name})")
                            else:
                                sess.travel_depart(_TRAVEL_LOCATION_ID)
                                departed += 1
                                details.append(f"acc{a.id}:已派出(地点{_TRAVEL_LOCATION_ID},{name})")
                        elif state == "traveling":
                            skipped += 1
                            details.append(f"acc{a.id}:旅行中({name}{_fmt_travel_eta(st)},record={st.get('record_id') or '-'})")
                        else:
                            skipped += 1  # 未知状态
                            details.append(f"acc{a.id}:未知状态{state!r}")
                finally:
                    # 无论中途是否失败，把可能已刷新的 token 写回
                    a.auth_json = sess.updated_json()
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")
            details.append(f"acc{a.id}:失败:{str(e)[:60]}")
        time.sleep(_ACCOUNT_DELAY)
    db.commit()
    return {"task": "cat_travel", "departed": departed, "claimed": claimed,
            "adopted": adopted, "skipped": skipped, "failed": failed,
            "details": details[:20], "errors": errors[:10]}


def run_activity_report(db, schedule: "Schedule | None" = None) -> dict:
    """对话活跃上报：点亮 growth 连登 + 解锁领猫任务（first_buddy 的 chat_5 门槛）。

    每号上报 N 条（默认 5，env ADMIN_ACTIVITY_REPORT_COUNT）共用同一
    conversationId（模拟同一会话内 N 轮对话），条间 1.5s，requestId 各条独立。
    发满后回读 streak 自检（days==0 说明上报被上游静默丢弃，通常= userId 缺失）；
    无猫账号发满后立即重试领养（对话量刚补满的新状态，豁免当日防抖）。

    风控口径：每号每天 1 轮（调度间隔 24h），不做多时点高频上报。
    """
    from admin.models import Account
    from admin import backend as be

    count = _activity_report_count()
    reported = failed = 0
    streak_warns: list[str] = []
    errors: list[str] = []
    details: list[str] = []
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            with be.AccountSession(a.auth_json) as sess:
                ok = 0
                try:
                    cid = f"wb2api-{int(time.time() * 1000)}"
                    for i in range(1, count + 1):
                        sess.report_chat_activity(cid, f"{cid}-r{i}")
                        ok += 1
                        if i < count:
                            time.sleep(_REPORT_GAP)
                finally:
                    a.auth_json = sess.updated_json()  # token 始终写回
                if ok < count:
                    errors.append(f"acc{a.id}:上报中断({ok}/{count})")
                    details.append(f"acc{a.id}:上报中断({ok}/{count})")
                    continue  # 未发满：streak 自检与补领养均无意义
                reported += 1
                # streak 自检（只读 oracle，失败不影响主流程）
                streak_note = ""
                try:
                    days = sess.growth_streak_days()
                    if days == 0:
                        streak_warns.append(f"acc{a.id}")  # 上报 OK 但连登为 0
                        streak_note = ",连登可疑(0,疑似静默丢弃)"
                    else:
                        streak_note = f",连登{days}天"
                except Exception:
                    streak_warns.append(f"acc{a.id}")
                    streak_note = ",连登回读失败"
                # 无猫账号：对话量刚补满 → 立即重试领养（豁免当日防抖）
                adopt_note = ""
                if sess.buddy_info() is None:
                    sess.buddy_agreement()
                    try:
                        sess.buddy_first()
                        adopt_note = ",补领养成功(+300)"
                    except Exception as e:
                        if _is_adopt_threshold_error(e):
                            _mark_adopt_tried(a.uid or "")
                            adopt_note = ",补领养门槛仍未达"
                        # 其他领养错误静默：上报已成功，不影响本轮结果
                details.append(f"acc{a.id}:上报{ok}/{count}{streak_note}{adopt_note}")
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")
            details.append(f"acc{a.id}:失败:{str(e)[:60]}")
        time.sleep(_ACCOUNT_DELAY)
    db.commit()
    return {"task": "activity_report", "reported": reported, "failed": failed,
            "count_per_account": count, "streak_warn": streak_warns,
            "details": details[:20], "errors": errors[:10]}


def _run_one(s: Schedule, db, now: datetime):
    try:
        result = run_task(s.task, db, s)
        s.last_result = json.dumps(result, ensure_ascii=False)[:2000]
    except Exception as e:  # 单个任务失败不影响调度循环
        s.last_result = f"执行失败: {e}"[:2000]
    s.last_run_at = now
    s.next_run_at = now + timedelta(minutes=s.interval_minutes or 60)
    db.commit()


def _loop():
    while True:
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            for s in db.query(Schedule).filter(Schedule.enabled == 1).all():
                if s.next_run_at is None or s.next_run_at <= now:
                    _run_one(s, db, now)
            db.close()
        except Exception:
            try:
                db.close()
            except Exception:
                pass
        time.sleep(15)


def seed_defaults(db):
    """首次启动若无任何任务则写入默认任务（含每日签到 / 猫猫旅行 / 活跃上报）。"""
    if db.query(Schedule).count() == 0:
        now = datetime.utcnow()
        db.add(Schedule(name="整点刷新平台总积分", task="refresh_balances",
                        interval_minutes=60, enabled=1, next_run_at=now))
        db.add(Schedule(name="每日同步模型列表", task="sync_models",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="每日签到领取积分", task="daily_checkin",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="猫猫旅行巡检", task="cat_travel",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="对话活跃上报", task="activity_report",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.commit()


def ensure_daily_checkin(db):
    """已存在其它任务但缺每日签到时，补一个默认签到任务（幂等）。

    保证「定期自动签到」在任意已运行实例上都有配置：今天已领的账号会被跳过，
    活动结束（EventEnded）时调度器自动把 stop_after 置为今天，不会误发请求触发风控。
    """
    if db.query(Schedule).filter(Schedule.task == "daily_checkin").count() == 0:
        now = datetime.utcnow()
        db.add(Schedule(name="每日签到领取积分", task="daily_checkin",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.commit()


def ensure_growth_tasks(db):
    """老实例缺猫猫旅行 / 活跃上报任务时幂等补充（与 ensure_daily_checkin 同理）。"""
    added = False
    now = datetime.utcnow()
    if db.query(Schedule).filter(Schedule.task == "cat_travel").count() == 0:
        db.add(Schedule(name="猫猫旅行巡检", task="cat_travel",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        added = True
    if db.query(Schedule).filter(Schedule.task == "activity_report").count() == 0:
        db.add(Schedule(name="对话活跃上报", task="activity_report",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        added = True
    if added:
        db.commit()


def start_scheduler():
    """在 FastAPI 启动时调用：播种默认任务并拉起守护线程。"""
    try:
        db = SessionLocal()
        seed_defaults(db)
        ensure_daily_checkin(db)
        ensure_growth_tasks(db)
        db.close()
    except Exception:
        pass
    t = threading.Thread(target=_loop, daemon=True, name="wb-scheduler")
    t.start()
