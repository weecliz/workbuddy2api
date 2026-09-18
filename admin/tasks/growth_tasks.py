"""成长任务全自动完成：批量接取 → 事件上报点亮 → 自动领奖。

契约对齐 workbuddy2api-hub wb_tasks.py 实测口径（见 admin/backend/growth.py
与 admin/tasks/event_specs.py 的说明）。本任务只管任务中心的接取/点亮/领奖；
连登（streak）与领猫解锁仍由 activity_report 负责，猫猫旅行由 cat_travel
负责，互不重叠。

分层（**手动补跑与定时调度必须共用同一份编排逻辑**）
----------------------------------------------------
    run_growth_for_account(acc, ...)   ← 单号：接取 → 点亮 → 领奖，返回结构化结果
    run_growth_tasks(db, ...)          ← 全量：遍历账号逐个调用上面那个 + 汇总

为什么不能各写一份：参考实现的 run_accounts docstring 记过一次真实坑——
绕过 accept 落库等待单独实现「单号快跑」会导致任务判不完成。手动补跑与定时跑
一旦分叉，行为差异极难排查，所以单号函数是唯一编排入口，全量只是它的循环。

风控口径：
  - 账号间 ACCOUNT_DELAY（0.8s，复用 common.py）
  - 同账号上报条间 REPORT_GAP（默认 1.5s，env ADMIN_GROWTH_REPORT_GAP）
  - 每号每天 1 轮（调度间隔 1440），不做多时点高频
  - 单任务上报次数 = target - current，不超额刷；失败不重试（留给下一轮）
  - 夜猫子（black_cat）仅在 CST 23:00-08:00 点亮，白天跳过
  - 总开关 ADMIN_GROWTH_TASK_ENABLED=0 一键停用

并发互斥（_RUN_LOCK）
---------------------
定时调度线程与 HTTP 手动补跑线程可以同时抵达这里。若不加锁，两路会：
  1) 对同一批账号同时打上游（风控面翻倍）；
  2) 并发写回同一个 a.auth_json（后写覆盖先写，丢掉对方的 token 刷新）。
故用模块级非阻塞锁：抢不到就直接返回「已有任务在跑」，绝不排队等待
（排队会让 HTTP 请求挂住，正是 jobrunner 要消除的形态）。
配套的 jobrunner.is_running 检查在 admin/scheduler.py，两者合起来才是完整互斥。
"""
import os
import threading
import time

from admin.backend import AccountSession
from admin.models import Account
from .common import ACCOUNT_DELAY
from . import event_specs

#: 单号交互明细上限（防超长结果撑爆 last_result / job 内存）
MAX_DETAILS = 40

#: 全量结果里保留多少个账号的汇总（与 schedules.last_result 截断上限配套）
MAX_ACCOUNTS_REPORTED = 200

#: 手动补跑与定时调度的互斥锁（非阻塞语义，见模块 docstring）
_RUN_LOCK = threading.Lock()


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


def run_growth_for_account(acc, task_codes=None) -> dict:
    """处理**单个**账号：接取 → 点亮 → 领奖，返回结构化结果。

    这是唯一的编排入口（全量与手动补跑共用），故这里不碰 db：token 回写靠
    调用方持有的 ORM 对象（`acc.auth_json = sess.updated_json()`），事务提交
    也由调用方负责。

    Args:
        acc: Account（或测试替身）；只用到 id / uid / name / auth_json
        task_codes: 限定只处理这些 task_code（None = 全部可自动任务）。
                    手动补跑单个任务时用；**仍会照常处理未接取状态**。

    Returns:
        {
          account_id, name, ok, error,
          accepted, lit, claimed, earned_credit, failed_tasks, skipped_night,
          scanned, done_tasks,             # 扫描到的 / 已领取的任务数
          summary,                         # 单行文本，如 "接1/点亮2/领1/+300"
          warning,                         # 有待办却一步没动时的告警（可 None）
          detail: [ {code,name,status,current,target,action,note} ],
        }

    注意：本函数**任何**单任务异常都不向外抛（只记进 detail/error），
    账号级异常才置 ok=False —— 与全量循环的「单号失败隔离」语义一致。
    """
    acc_id = getattr(acc, "id", 0)
    uid = getattr(acc, "uid", "") or ""
    name = getattr(acc, "name", "") or ""

    res: dict = {
        "account_id": acc_id,
        "name": name,
        "ok": True,
        "error": None,
        "accepted": 0, "lit": 0, "claimed": 0, "earned_credit": 0,
        "failed_tasks": 0, "skipped_night": 0,
        "scanned": 0, "done_tasks": 0,
        "summary": "", "warning": None,
        "detail": [],
    }
    gap = report_gap()
    detail: list[dict] = res["detail"]
    want = set(task_codes) if task_codes else None

    def note(code, name_, status, cur, tgt, action, msg=""):
        if len(detail) < MAX_DETAILS:
            detail.append({"code": code, "name": name_, "status": status,
                           "current": cur, "target": tgt,
                           "action": action, "note": msg})

    try:
        with AccountSession(acc.auth_json) as sess:
            tasks = sess.fetch_tasks()
            if not tasks:
                res["ok"] = False
                res["error"] = "任务清单为空(网络或账号状态)"
                note("-", "-", "-", 0, 0, "error", res["error"])
                return res

            if want is not None:
                tasks = [t for t in tasks if t["task_code"] in want]
            res["scanned"] = len(tasks)

            # 1. 批量接取未接且可自动完成的任务
            unaccepted = [t["task_code"] for t in tasks
                          if t["status"] == "not_accepted"
                          and not event_specs.is_skipped(t["task_code"])]
            if unaccepted:
                sess.accept_tasks(unaccepted)
                res["accepted"] = len(unaccepted)
                time.sleep(1.0)
                tasks = sess.fetch_tasks()  # 接取后重拉，拿最新进度
                if want is not None:
                    tasks = [t for t in tasks if t["task_code"] in want]

            # 2. 逐任务处理
            pending = 0
            for t in tasks:
                code = t["task_code"]
                spec = event_specs.TASK_SPECS.get(code) or {}
                name_ = t.get("name") or spec.get("name") or code
                cur = t.get("current", 0)
                tgt = t.get("target", 1)

                if t["status"] == "claimed":
                    res["done_tasks"] += 1
                    note(code, name_, t["status"], cur, tgt, "skip", "已领取")
                    continue
                if event_specs.is_skipped(code):
                    note(code, name_, t["status"], cur, tgt, "skip",
                         "不可自动完成(需人工/归其它任务管)")
                    continue

                pending += 1
                kind = spec.get("kind") or ""

                # 夜猫子：仅夜间点亮，白天跳过（留给下一轮调度）
                if kind in event_specs.NIGHT_KINDS and not event_specs.in_night_window():
                    res["skipped_night"] += 1
                    note(code, name_, t["status"], cur, tgt, "night", "待夜间窗口")
                    continue

                lit_here = 0
                if t["status"] != "completed" and cur < tgt:
                    # 点亮：补足缺口次数的事件上报
                    need = tgt - cur
                    try:
                        for i in range(need):
                            sess.report_events([event_specs.build_event(uid, kind, idx=i)])
                            lit_here += 1
                            if i < need - 1:
                                time.sleep(gap)
                        res["lit"] += need
                        time.sleep(1.5)
                    except Exception as e:
                        # 上报中断：已发出去的无法回收，如实记数并继续下一个任务
                        res["lit"] += lit_here
                        res["failed_tasks"] += 1
                        note(code, name_, t["status"], cur, tgt, "error",
                             f"点亮中断({lit_here}/{need}): {str(e)[:40]}")
                        time.sleep(gap)
                        continue

                # 领奖（completed / 刚点亮）
                try:
                    r = sess.claim_task(code)
                except Exception as e:
                    res["failed_tasks"] += 1
                    note(code, name_, t["status"], cur, tgt, "error",
                         f"领奖异常: {str(e)[:40]}")
                    time.sleep(gap)
                    continue
                if r.get("ok"):
                    credit = r.get("credit", 0)
                    res["earned_credit"] += credit
                    res["claimed"] += 1
                    res["done_tasks"] += 1
                    note(code, name_, "claimed", cur, tgt, "claim", f"+{credit}")
                else:
                    # 上报成功但领奖被拒（如需结算延迟）：记录，不算整体失败
                    note(code, name_, t["status"], cur, tgt, "claim_pending",
                         f"领奖待结算({str(r.get('msg', ''))[:30]})")
                time.sleep(gap)

            acc.auth_json = sess.updated_json()  # token 始终写回

            # 有待办却一步没动 → 上游可能改版（可视化信号，见 docs/TASKS.md）
            if pending and not (res["accepted"] or res["lit"] or res["claimed"]):
                if res["skipped_night"] == pending:
                    # 全是夜猫子且不在窗口内：预期行为，不是异常
                    pass
                else:
                    res["warning"] = "本轮无进展(上游改版?)"

        res["summary"] = (f"接{res['accepted']}/点亮{res['lit']}"
                          f"/领{res['claimed']}/+{res['earned_credit']}")
    except Exception as e:
        res["ok"] = False
        res["error"] = str(e)[:120]
        res["summary"] = f"失败: {res['error'][:60]}"

    return res


def run_growth_tasks(db, schedule=None, account_ids=None, task_codes=None,
                     on_step=None, include_detail=False) -> dict:
    """全量（或指定账号）：遍历 active 账号，逐个调用 run_growth_for_account。

    Args:
        db: SQLAlchemy 会话（由调用方持有与提交）
        schedule: Schedule 行（当前未用到，保留签名与其它任务一致）
        account_ids: 只处理这些账号 id（None = 全部 active）。手动补跑用。
        task_codes: 透传给单号函数，限定任务集合。
        on_step: 每处理完一个账号回调一次（异步任务用它上报进度）。
        include_detail: accounts 里是否带逐任务 detail。
            **默认 False**：定时任务的结果要落进 schedules.last_result（截断
            8000 字符），13 个账号 × 每号几十条 detail 会轻松超限，截断后 JSON
            不完整、前端 JSON.parse 失败（只剩半截文本）。手动补跑的 job 存在
            内存里不怕大，故传 True 以支持弹窗逐任务展示。

    Returns:
        除原有全局计数外，新增**按账号维度的汇总**：
          "accounts": [{account_id,name,ok,accepted,lit,claimed,earned_credit,
                        scanned,done_tasks,skipped_night,failed_tasks,
                        summary,warning,error, (detail 可选)}, ...]
          "accounts_total": 实际处理的账号数
          "accounts_failed": 整体失败的账号数
          "truncated": 为 True 表示 accounts 被截断（见 MAX_ACCOUNTS_REPORTED）
        `details` 保留为逐号单行文本（兼容旧前端与日志阅读习惯）。
    """
    if not growth_enabled():
        return {"task": "growth_tasks", "skipped": "总开关已关闭"
                "(ADMIN_GROWTH_TASK_ENABLED=0)"}

    if not _RUN_LOCK.acquire(blocking=False):
        # 手动补跑与定时调度撞车：后到的不排队，直接让路（见模块 docstring）
        return {"task": "growth_tasks",
                "skipped": "已有成长任务在执行(手动或定时)，本轮跳过"}

    try:
        # pi-lens-ignore: python-sql-injection
        rows = db.query(Account).filter(Account.status == "active").all()
        if account_ids is not None:
            wanted = set(account_ids)
            rows = [a for a in rows if getattr(a, "id", None) in wanted]

        accepted_total = lit_total = claimed_total = earned = failed = 0
        errors: list[str] = []
        details: list[str] = []
        accounts: list[dict] = []

        for a in rows:
            if on_step:
                try:
                    on_step(getattr(a, "name", "") or f"acc{getattr(a, 'id', 0)}")
                except Exception:
                    pass  # 进度回调失败不影响主流程
            r = run_growth_for_account(a, task_codes)
            accepted_total += r["accepted"]
            lit_total += r["lit"]
            claimed_total += r["claimed"]
            earned += r["earned_credit"]
            if not r["ok"]:
                failed += 1
                errors.append(f"acc{r['account_id']}:{r['error']}")
            # 按账号维度的汇总（detail 按 include_detail 决定是否保留）
            if len(accounts) < MAX_ACCOUNTS_REPORTED:
                accounts.append({k: v for k, v in r.items()
                                 if include_detail or k != "detail"})
            details.append(f"acc{r['account_id']}:{r['summary']}")
            time.sleep(ACCOUNT_DELAY)

        db.commit()
        return {
            "task": "growth_tasks",
            "accepted": accepted_total, "lit": lit_total,
            "claimed": claimed_total, "earned_credit": earned, "failed": failed,
            "accounts_total": len(rows),
            "accounts_failed": failed,
            "accounts": accounts,
            "truncated": len(rows) > MAX_ACCOUNTS_REPORTED,
            # 逐号单行文本：summary 与 accounts 内容重复，但阅读日志时更省事
            "details": details[:MAX_ACCOUNTS_REPORTED],
            "errors": errors[:10],
        }
    finally:
        _RUN_LOCK.release()


# ---------------------------------------------------------------------------
# 只读：单账号任务清单（供前端「任务详情」弹窗，不产生任何上游写入）
# ---------------------------------------------------------------------------
def describe_account_tasks(acc) -> dict:
    """实时拉取**单个**账号的任务清单并分类，供前端展示。**只读**。

    与 run_growth_for_account 的关键差别：这里不接取、不上报、不领奖；
    唯一的写回是把可能被刷新的 token 存回 acc.auth_json（否则同一请求内的
    后续调用会用过期的 access token）。**不 commit**，由调用方决定。

    `action` 的判定分支**必须与 run_growth_for_account 逐条对齐**（见下方
    注释里的行号对应），否则前端会显示「可补跑」而补跑什么都不做。

    Returns:
        {
          account_id, name, uid,
          tasks: [{code,name,status,current,target,reward,action,note}],
          summary: {total,claimed,claimable,claimable_credit,actionable,
                    night_pending,manual,done},
          errors: [...],   # 拉取失败时非空
        }

    action 取值（前端据此决定是否显示「补跑」）：
        done        已领取 —— 无可做
        manual      不可自动完成（需人工 / 归其它任务管）—— 补跑无用
        night       待夜间窗口（black_cat）—— 白天补跑也点不亮
        claimable   已完成待领取 —— 补跑能立刻拿分
        actionable  可自动推进（未接取 / 未达标）—— 补跑有意义
    """
    out: dict = {"account_id": getattr(acc, "id", 0),
                 "name": getattr(acc, "name", "") or "",
                 "uid": getattr(acc, "uid", "") or "",
                 "tasks": [], "summary": {}, "errors": []}
    try:
        with AccountSession(acc.auth_json) as sess:
            raw = sess.fetch_tasks()
            acc.auth_json = sess.updated_json()  # token 可能被刷新，必须写回
    except Exception as e:
        out["errors"].append(str(e)[:120])
        return out

    night = event_specs.in_night_window()
    tasks: list[dict] = []
    s = {"total": 0, "claimed": 0, "claimable": 0, "claimable_credit": 0,
         "actionable": 0, "night_pending": 0, "manual": 0, "done": 0}

    for t in raw:
        code = t["task_code"]
        spec = event_specs.TASK_SPECS.get(code) or {}
        status = t.get("status") or ""
        cur, tgt = t.get("current", 0), t.get("target", 1)
        reward = t.get("reward_credit", 0)

        # 下面四个分支的顺序与 run_growth_for_account 的循环体一致：
        # claimed → is_skipped → 夜猫窗口 → completed/未达标
        if status == "claimed":
            action, note = "done", "已领取"
            s["claimed"] += 1
            s["done"] += 1
        elif event_specs.is_skipped(code):
            action, note = "manual", "不可自动完成(需人工/归其它任务管)"
            s["manual"] += 1
        elif spec.get("kind") in event_specs.NIGHT_KINDS and not night:
            action, note = "night", "待夜间窗口(23:00-08:00)"
            s["night_pending"] += 1
        elif status == "completed":
            action, note = "claimable", "已完成待领取"
            s["claimable"] += 1
            s["claimable_credit"] += reward
        else:
            action, note = "actionable", f"待推进 {cur}/{tgt}"
            s["actionable"] += 1

        s["total"] += 1
        tasks.append({"code": code,
                      "name": t.get("name") or spec.get("name") or code,
                      "status": status, "current": cur, "target": tgt,
                      "reward": reward, "action": action, "note": note})

    out["tasks"] = tasks
    out["summary"] = s
    return out
