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
import random
import threading
import time
from datetime import datetime, timedelta

from admin.db import SessionLocal
from admin.jobrunner import KEY_GROWTH, RUNNER
from admin.models import Schedule
from admin.tasks import run_activity_report, run_cat_travel, run_daily_checkin, run_growth_tasks

#: last_result 落库前的截断长度。
#: 从 2000 提到 8000：成长任务现在要存**按账号维度的汇总**（accounts 数组），
#: 10 个账号的逐号对象就超过 2000，被截断后 JSON 不完整、前端 JSON.parse 失败
#: （表现为结果栏只剩半截文本）。路由侧同一常量见 admin/routers/schedules.py。
LAST_RESULT_MAX = 8000


def run_task(task: str, db, schedule: "Schedule | None" = None) -> dict:
    """执行某个任务，返回结果摘要字典。"""
    if task == "refresh_balances":
        from admin.routers import accounts as acc_router
        from admin.models import Account
        ok = fail = 0
        # 账号间随机延迟 2~5s：这是唯一高频任务（每小时）+ 之前零间隔，
        # 13 个号 2 个请求连发是最机器化的风控形态。抖动打散等间距特征。
        # 13 个号一轮多花 ≤1 分钟，每小时跑一次完全无感。
        first = True
        # pi-lens-ignore: python-sql-injection
        for a in db.query(Account).filter(Account.status == "active").all():
            if not first:
                time.sleep(random.uniform(2.0, 5.0))
            first = False
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
    if task == "growth_tasks":
        return run_growth_tasks(db, schedule)
    return {"task": task, "error": "未知任务类型"}


def _run_one(s: Schedule, db, now: datetime):
    # 成长任务与「手动补跑」（/api/growth/run）可能撞车：两路同时遍历同一批账号
    # 会双倍打上游（风控面翻倍）并并发写回同一个 auth_json（后写覆盖先写，
    # 丢掉对方的 token 刷新）。jobrunner 的同 key 只能防「两次手动」，
    # 防不住「手动 vs 定时」，故在此让路。
    if s.task == "growth_tasks" and RUNNER.is_running(KEY_GROWTH):
        s.last_result = json.dumps(
            {"task": "growth_tasks",
             "skipped": "已有手动补跑在执行，本轮跳过"}, ensure_ascii=False)
        s.last_run_at = now
        s.next_run_at = now + timedelta(minutes=s.interval_minutes or 60)
        db.commit()
        return
    try:
        # task 列在库里可空（历史遗留），但业务上必有值；给个空串兜底，
        # run_task 会把它归为「未知任务类型」并记入 last_result，不会静默失败。
        result = run_task(s.task or "", db, s)
        s.last_result = json.dumps(result, ensure_ascii=False)[:LAST_RESULT_MAX]
    except Exception as e:  # 单个任务失败不影响调度循环
        s.last_result = f"执行失败: {e}"[:LAST_RESULT_MAX]
    s.last_run_at = now
    s.next_run_at = now + timedelta(minutes=s.interval_minutes or 60)
    db.commit()


def _loop():
    while True:
        try:
            # 先只读地收由到期任务 id 列表（短事务，立刻释放连接），
            # 执行阶段再逐任务开会话 —— 错峰 sleep 最长 150s，若在长 sleep
            # 期间挂着同一连接，MySQL 会把空闲连接断掉，后续 _run_one 写库报错。
            db = SessionLocal()
            try:
                now = datetime.utcnow()
                due_ids = [s.id for s in db.query(Schedule).filter(
                    Schedule.enabled == 1).all()
                    if s.next_run_at is None or s.next_run_at <= now]
            finally:
                db.close()

            for i, sid in enumerate(due_ids):
                if i:
                    # 同一轮到期的多个任务错峰执行：播种 / 手动触发 / 相同 interval
                    # 都会让几个任务同刻到期，背靠背执行意味着同一账号在几十秒内
                    # 被 签到→旅行→上报→成长 连续打 4 轮，形态过于机器。
                    # 不改 next_run_at（按次加随机偏移会逐日累积漂移），在执行层拉开。
                    time.sleep(random.uniform(60, 150))
                db = SessionLocal()
                try:
                    s = db.query(Schedule).filter(Schedule.id == sid).first()
                    # 等待期间可能被删除 / 停用 / 手动触发过（next_run_at 已后移）
                    if s is None or not s.enabled:
                        continue
                    if s.next_run_at is not None and s.next_run_at > datetime.utcnow():
                        continue
                    _run_one(s, db, datetime.utcnow())
                finally:
                    db.close()
        except Exception:
            # SessionLocal() / 查询自身的异常（数据库不可用 / 驱动问题）就地吞掉：
            # 调度线程不能死，下一轮 15s 后重试。任务级异常已在 _run_one 内隔离。
            pass
        time.sleep(15)


def seed_defaults(db):
    """首次启动若无任何任务则写入默认任务（含每日签到 / 猫猫旅行 / 活跃上报）。"""
    # pi-lens-ignore: python-sql-injection
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
    # pi-lens-ignore: python-sql-injection
    if db.query(Schedule).filter(Schedule.task == "daily_checkin").count() == 0:
        now = datetime.utcnow()
        db.add(Schedule(name="每日签到领取积分", task="daily_checkin",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.commit()


def ensure_growth_tasks(db):
    """老实例缺猫猫旅行 / 活跃上报任务时幂等补充（与 ensure_daily_checkin 同理）。"""
    added = False
    now = datetime.utcnow()
    # pi-lens-ignore: python-sql-injection
    if db.query(Schedule).filter(Schedule.task == "cat_travel").count() == 0:
        db.add(Schedule(name="猫猫旅行巡检", task="cat_travel",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        added = True
    # pi-lens-ignore: python-sql-injection
    if db.query(Schedule).filter(Schedule.task == "activity_report").count() == 0:
        db.add(Schedule(name="对话活跃上报", task="activity_report",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        added = True
    # pi-lens-ignore: python-sql-injection
    if db.query(Schedule).filter(Schedule.task == "growth_tasks").count() == 0:
        db.add(Schedule(name="成长任务点亮领奖", task="growth_tasks",
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
