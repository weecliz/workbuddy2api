"""成长任务全自动完成：批量接取 → 事件上报点亮 → 自动领奖。

契约对齐 workbuddy2api-hub wb_tasks.py 实测口径（见 admin/backend/growth.py
与 admin/tasks/event_specs.py 的说明）。本任务只管任务中心的接取/点亮/领奖；
连登（streak）与领猫解锁仍由 activity_report 负责，猫猫旅行由 cat_travel
负责，互不重叠。

风控口径：
  - 账号间 ACCOUNT_DELAY（0.8s，复用 common.py）
  - 同账号上报条间 REPORT_GAP（默认 1.5s，env ADMIN_GROWTH_REPORT_GAP）
  - 每号每天 1 轮（调度间隔 1440），不做多时点高频
  - 单任务上报次数 = target - current，不超额刷；失败不重试（留给下一轮）
  - 夜猫子（black_cat）仅在 CST 23:00-08:00 点亮，白天跳过
  - 总开关 ADMIN_GROWTH_TASK_ENABLED=0 一键停用
"""
import os
import time

from admin.backend import AccountSession
from admin.models import Account
from .common import ACCOUNT_DELAY
from . import event_specs


def report_gap() -> float:
    """同账号上报条间隔（秒），env ADMIN_GROWTH_REPORT_GAP，默认 1.5；非法/<=0 回落默认。"""
    try:
        gap = float(os.getenv("ADMIN_GROWTH_REPORT_GAP", "1.5"))
    except (TypeError, ValueError):
        return 1.5
    return gap if gap > 0 else 1.5


def growth_enabled() -> bool:
    """总开关（env ADMIN_GROWTH_TASK_ENABLED，默认开）。仅作逃生口。"""
    return os.getenv("ADMIN_GROWTH_TASK_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def run_growth_tasks(db, schedule=None) -> dict:
    if not growth_enabled():
        return {"task": "growth_tasks", "skipped": "总开关已关闭"
                "(ADMIN_GROWTH_TASK_ENABLED=0)"}

    accepted_total = lit_total = claimed_total = earned = failed = 0
    errors: list[str] = []
    details: list[str] = []
    gap = report_gap()

    # pi-lens-ignore: python-sql-injection
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            acc_accepted = acc_lit = acc_claimed = 0
            acc_earned = 0
            with AccountSession(a.auth_json) as sess:
                uid = (a.uid or "")
                tasks = sess.fetch_tasks()
                if not tasks:
                    details.append(f"acc{a.id}:任务清单为空(网络或账号状态)")
                    continue

                # 1. 批量接取未接且可自动完成的任务
                unaccepted = [t["task_code"] for t in tasks
                              if t["status"] == "not_accepted"
                              and not event_specs.is_skipped(t["task_code"])]
                if unaccepted:
                    sess.accept_tasks(unaccepted)
                    accepted_total += len(unaccepted)
                    acc_accepted = len(unaccepted)
                    time.sleep(1.0)
                    tasks = sess.fetch_tasks()  # 接取后重拉，拿最新进度

                # 2. 逐任务处理
                for t in tasks:
                    code = t["task_code"]
                    if event_specs.is_skipped(code):
                        continue
                    spec = event_specs.TASK_SPECS.get(code) or {}
                    kind = spec.get("kind") or ""
                    name = t.get("name") or spec.get("name") or code

                    if t["status"] == "claimed":
                        continue

                    # 夜猫子：仅夜间点亮，白天跳过（留给下一轮调度）
                    if kind in event_specs.NIGHT_KINDS and not event_specs.in_night_window():
                        details.append(f"acc{a.id}:{name},待夜间窗口")
                        continue

                    cur, tgt = t.get("current", 0), t.get("target", 1)
                    if t["status"] != "completed" and cur < tgt:
                        # 点亮：补足缺口次数的事件上报
                        need = tgt - cur
                        for i in range(need):
                            sess.report_events([event_specs.build_event(uid, kind, idx=i)])
                            if i < need - 1:
                                time.sleep(gap)
                        lit_total += need
                        acc_lit += need
                        time.sleep(1.5)

                    # 领奖（completed / 刚点亮）
                    res = sess.claim_task(code)
                    if res.get("ok"):
                        credit = res.get("credit", 0)
                        earned += credit
                        acc_earned += credit
                        claimed_total += 1
                        acc_claimed += 1
                        details.append(f"acc{a.id}:{name},+{credit}")
                    else:
                        # 上报成功但领奖被拒（如需结算延迟）：记录，不算整体失败
                        details.append(f"acc{a.id}:{name},领奖待结算({res.get('msg', '')[:30]})")
                    time.sleep(gap)

                a.auth_json = sess.updated_json()  # token 始终写回
            if acc_accepted or acc_lit or acc_claimed:
                details.append(f"acc{a.id}:本轮接{acc_accepted}/点亮{acc_lit}/领{acc_claimed}/+{acc_earned}")
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")
            details.append(f"acc{a.id}:失败:{str(e)[:60]}")
        time.sleep(ACCOUNT_DELAY)

    db.commit()
    return {"task": "growth_tasks", "accepted": accepted_total, "lit": lit_total,
            "claimed": claimed_total, "earned_credit": earned, "failed": failed,
            "details": details[:20], "errors": errors[:10]}
