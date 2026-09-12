"""轻量后台定时任务调度器（无第三方依赖）。

用守护线程每 15s 轮询 schedules 表，到点（next_run_at <= now）的任务就执行，
执行完更新 last_run_at / next_run_at / last_result。

**任务实现不在这里** —— 一个任务一个文件放在 admin/tasks/（见该包说明）。
本模块只管三件事：
  - run_task：按 task 名分发（refresh_balances / sync_models 是对路由的两行转发）
  - _run_one / _loop：到点执行、结果截断落库、异常不影响调度循环
  - seed_defaults / ensure_*：默认任务播种（幂等，老实例升级时补种）

支持的任务：
  - refresh_balances：遍历 active 账号刷新余额（统计平台总积分）
  - sync_models：从后端拉取最新模型列表并 upsert 倍率
  - daily_checkin：每日签到领取积分
  - cat_travel：猫猫旅行巡检（领养 / 派出 / 领奖状态机）
  - activity_report：对话活跃上报（点亮连登 + 解锁领猫任务）
"""
import json
import threading
import time
from datetime import datetime, timedelta

from admin.db import SessionLocal
from admin.models import Schedule
from admin.tasks import run_activity_report, run_cat_travel, run_daily_checkin


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
