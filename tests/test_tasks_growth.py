"""成长任务编排测试（FakeSess 模式，对齐 test_tasks_cat_travel.py）。

覆盖：
  状态机分支 → not_accepted 批量接取 / 进行中点亮 N 次 / completed 直接领 / claimed 跳过
  跳过规则   → SKIP_KINDS（buddy5/unforgeable/first_buddy 等）不产生任何上游调用
  夜猫时段   → black_cat 白天跳过、夜间点亮
  失败隔离   → 单号 fetch 抛异常不影响其余账号
  字段完整性 → build_event 输出含 userId/eventCode/timestamp
  上报体形状 → report_events 收单条也是数组
"""
import pytest
from datetime import datetime

from admin.tasks import growth_tasks as gt
from admin.tasks import event_specs as es
from admin.tasks import common


class FakeSess:
    """按 scenario 回放上游响应；记录调用便于断言。

    scenario 关键字：
      tasks       任务列表（或 "raise" 抛异常）
      tasks_after 接取后重拉返回的列表（缺省回 tasks）
      claim_ok    领奖是否成功
    """

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.calls: list[str] = []
        self.reported_events: list[list[dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def updated_json(self):
        return "{}"

    def fetch_tasks(self):
        self.calls.append("fetch_tasks")
        tasks = self.scenario.get("tasks")
        if tasks == "raise":
            raise RuntimeError("fetch failed")
        if self.calls.count("fetch_tasks") > 1 and "tasks_after" in self.scenario:
            return self.scenario["tasks_after"]
        return tasks

    def accept_tasks(self, codes):
        self.calls.append(f"accept_tasks:{','.join(codes)}")

    def report_events(self, events):
        assert isinstance(events, list), "上报体必须是数组"
        self.calls.append(f"report_events:{len(events)}")
        self.reported_events.extend(events)

    def claim_task(self, code):
        self.calls.append(f"claim_task:{code}")
        if self.scenario.get("claim_ok", True):
            return {"ok": True, "credit": 100, "energy": 0}
        return {"ok": False, "msg": "not ready"}


class FakeAccount:
    def __init__(self, id, uid, scenario):
        self.id = id
        self.uid = uid
        self.auth_json = f"auth-{id}"
        self.status = "active"
        self.scenario = scenario


class FakeQuery:
    def __init__(self, items):
        self.items = items

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.items


class FakeDB:
    def __init__(self, accounts):
        self.accounts = accounts
        self.commits = 0

    def query(self, model):
        return FakeQuery(self.accounts)

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gt.time, "sleep", lambda *a, **k: None)


def run(db, monkeypatch):
    made = []

    def fake_session(auth_json):
        acc = next(a for a in db.accounts if a.auth_json == auth_json)
        sess = FakeSess(acc.scenario)
        made.append((acc, sess))
        return sess

    monkeypatch.setattr(gt, "AccountSession", fake_session)
    return gt.run_growth_tasks(db), made


def run_one(acc, monkeypatch):
    """直跑**单号**入口（run_growth_for_account）。

    单号函数是「手动补跑」与「定时全量」共用的唯一编排入口（P1 的核心），
    而且只有它的返回值带逐任务 detail（全量默认 include_detail=False，
    以免超出 schedules.last_result 的 8000 字符上限）。
    返回 (结果, [sess])。
    """
    made = []

    def fake_session(auth_json):
        sess = FakeSess(acc.scenario)
        made.append(sess)
        return sess

    monkeypatch.setattr(gt, "AccountSession", fake_session)
    return gt.run_growth_for_account(acc), made


def T(code, status="accepted", cur=0, tgt=1, reward=100):
    return {"task_code": code, "name": code, "status": status,
            "current": cur, "target": tgt, "reward_credit": reward}


# ---------------------------------------------------------------------------
# 状态机分支
# ---------------------------------------------------------------------------

def test_not_accepted_then_accept_lit_claim(monkeypatch):
    """未接取 → 接取 → 重拉 → 点亮缺口次数 → 领奖。"""
    db = FakeDB([FakeAccount(1, "u1", {
        "tasks": [T("create_canvas", "not_accepted", tgt=2)],
        "tasks_after": [T("create_canvas", "accepted", cur=0, tgt=2)],
    })])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert "accept_tasks:create_canvas" in calls
    assert "report_events:1" in calls  # 缺口 2-0=2? 不：重拉后 target=2，点亮 2 次
    assert calls.count("report_events:1") == 2
    assert "claim_task:create_canvas" in calls
    assert res["claimed"] == 1 and res["earned_credit"] == 100


def test_completed_claims_directly_without_reporting(monkeypatch):
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]})])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert not any(c.startswith("report_events") for c in calls)
    assert "claim_task:create_canvas" in calls
    assert res["claimed"] == 1


def test_claimed_task_skipped(monkeypatch):
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("create_canvas", "claimed")]})])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert not any(c.startswith("claim_task") for c in calls)
    assert res["claimed"] == 0


# ---------------------------------------------------------------------------
# 跳过规则
# ---------------------------------------------------------------------------

def test_skip_kinds_never_touch_upstream(monkeypatch):
    codes = ["Buddy_App", "RichMeow_Chat", "Library_read", "first_buddy",
             "Expert_Philanthropy", "unknown_new_task"]
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T(c) for c in codes]})])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert not any(c.startswith("report_events") for c in calls)
    assert not any(c.startswith("claim_task") for c in calls)
    assert not any(c.startswith("accept_tasks") for c in calls)
    assert res["claimed"] == 0


def test_is_skipped_covers_unknown_codes():
    assert es.is_skipped("Expert_Philanthropy")
    assert es.is_skipped("first_buddy")
    assert es.is_skipped("some_brand_new_task")  # 未知任务不盲报
    assert not es.is_skipped("create_canvas")


# ---------------------------------------------------------------------------
# 夜猫时段
# ---------------------------------------------------------------------------

def test_black_cat_skipped_in_daytime(monkeypatch):
    # CST 12:00 = UTC 04:00 → 白天
    day = datetime(2026, 5, 20, 4, 0, 0)
    real_fn = es.in_night_window          # 先保存原函数，避免 lambda 递归调用自身
    monkeypatch.setattr(es, "in_night_window", lambda now_utc=None: real_fn(day))
    acc = FakeAccount(1, "u1", {"tasks": [T("black_cat", "accepted", cur=0, tgt=1)]})
    res, made = run_one(acc, monkeypatch)
    calls = made[0].calls
    assert not any(c.startswith("report_events") for c in calls)
    assert "claim_task:black_cat" not in calls
    # 断言载体从 details 文本改为结构化字段：夜猫子跳过有专列计数，
    # 比匹配中文文案稳（改文案不会弄失效测试）
    assert res["skipped_night"] == 1
    assert res["lit"] == 0 and res["claimed"] == 0
    assert any("待夜间" in d["note"] for d in res["detail"])


def test_black_cat_lit_at_night(monkeypatch):
    # CST 01:00 = UTC 前一日 17:00 → 夜间
    monkeypatch.setattr(es, "in_night_window", lambda now_utc=None: True)
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("black_cat", "accepted", cur=0, tgt=1)]})])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert "report_events:1" in calls
    assert "claim_task:black_cat" in calls


def test_in_night_window_boundaries():
    # CST 23:00 → 夜间；CST 08:00 → 非夜间；CST 22:59 → 非夜间
    assert es.in_night_window(datetime(2026, 5, 20, 15, 0))       # UTC 15:00 = CST 23:00
    assert not es.in_night_window(datetime(2026, 5, 20, 0, 0))    # UTC 00:00 = CST 08:00
    assert not es.in_night_window(datetime(2026, 5, 20, 14, 59))  # CST 22:59
    assert es.in_night_window(datetime(2026, 5, 20, 16, 0))       # UTC 16:00 = CST 次日 00:00


# ---------------------------------------------------------------------------
# 失败隔离
# ---------------------------------------------------------------------------

def test_account_failure_does_not_block_others(monkeypatch):
    db = FakeDB([
        FakeAccount(1, "u1", {"tasks": "raise"}),
        FakeAccount(2, "u2", {"tasks": [T("create_canvas", "completed")]}),
    ])
    res, made = run(db, monkeypatch)
    assert res["failed"] == 1
    assert res["claimed"] == 1  # 2 号照常领奖
    assert len(made) == 2


def test_claim_rejection_recorded_not_fatal(monkeypatch):
    acc = FakeAccount(1, "u1", {
        "tasks": [T("create_canvas", "completed")], "claim_ok": False})
    res, made = run_one(acc, monkeypatch)
    # 领奖被拒属预期（上游结算延迟）：不算账号失败、不算任务失败，但必须留痕
    assert res["ok"] and res["claimed"] == 0 and res["failed_tasks"] == 0
    assert any("待结算" in d["note"] for d in res["detail"])


# ---------------------------------------------------------------------------
# build_event 字段完整性
# ---------------------------------------------------------------------------

def test_build_event_required_fields():
    for kind in ("canvas", "template", "expert", "team", "lighthouse", "skill",
                 "automation", "playbook", "skin", "chat", "glmchat", "cat"):
        ev = es.build_event("uid-1", kind, idx=0)
        assert ev.get("userId") == "uid-1", kind
        assert ev.get("eventCode"), kind
        assert ev.get("timestamp"), kind


def test_build_event_cat_uses_night_mode():
    ev = es.build_event("uid-1", "cat")
    assert ev["mode"] == "night"
    assert ev["requestModelId"] == "glm-5.2"


# ---------------------------------------------------------------------------
# 总开关
# ---------------------------------------------------------------------------

def test_master_switch_off(monkeypatch):
    monkeypatch.setenv("ADMIN_GROWTH_TASK_ENABLED", "0")
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]})])
    res = gt.run_growth_tasks(db)
    assert res.get("skipped")
    assert db.commits == 0


# ---------------------------------------------------------------------------
# P1：单号入口与按账号维度汇总
# ---------------------------------------------------------------------------

def test_per_account_summary_shape(monkeypatch):
    """全量结果必须带按账号维度的汇总（本次需求的核心）。"""
    db = FakeDB([
        FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]}),
        FakeAccount(2, "u2", {"tasks": "raise"}),          # 失败隔离
    ])
    res, _ = run(db, monkeypatch)
    assert res["accounts_total"] == 2
    assert res["accounts_failed"] == 1
    assert len(res["accounts"]) == 2
    a1, a2 = res["accounts"]
    # 汇总项字段齐全，前端表格直接用
    for k in ("account_id", "name", "ok", "accepted", "lit", "claimed",
              "earned_credit", "scanned", "done_tasks", "summary"):
        assert k in a1, k
    assert a1["ok"] and a1["claimed"] == 1 and a1["earned_credit"] == 100
    assert a2["ok"] is False and "fetch failed" in (a2["error"] or "")
    # 默认不带逐任务 detail（防止超 last_result 上限）
    assert "detail" not in a1


def test_detail_opt_in_for_async_job(monkeypatch):
    """手动补跑（异步 job）需要逐任务 detail，由 include_detail 打开。"""
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]})])
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(db.accounts[0].scenario))
    res = gt.run_growth_tasks(db, include_detail=True)
    assert "detail" in res["accounts"][0]
    assert res["accounts"][0]["detail"][0]["code"] == "create_canvas"


def test_run_for_single_account_only_touches_it(monkeypatch):
    """手动补跑单个账号：只请求该号，其它号零调用。"""
    db = FakeDB([
        FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]}),
        FakeAccount(2, "u2", {"tasks": [T("create_canvas", "completed")]}),
    ])
    made = []

    def fake_session(auth_json):
        acc = next(a for a in db.accounts if a.auth_json == auth_json)
        sess = FakeSess(acc.scenario)
        made.append(acc)
        return sess

    monkeypatch.setattr(gt, "AccountSession", fake_session)
    res = gt.run_growth_tasks(db, account_ids=[2])
    assert res["accounts_total"] == 1
    assert [a.id for a in made] == [2]          # 1 号完全没被碰
    assert res["accounts"][0]["account_id"] == 2
    assert res["claimed"] == 1


def test_task_codes_filters_to_subset(monkeypatch):
    """限定 task_codes 时只处理这些任务（手动补跑单个任务用）。"""
    acc = FakeAccount(1, "u1", {"tasks": [
        T("create_canvas", "completed"),
        T("skill_1", "completed"),
    ]})
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(acc.scenario))
    res = gt.run_growth_for_account(acc, task_codes=["skill_1"])
    assert res["scanned"] == 1
    assert res["claimed"] == 1
    codes = [d["code"] for d in res["detail"]]
    assert codes == ["skill_1"]


def test_on_step_called_per_account(monkeypatch):
    """异步任务靠 on_step 上报「正在处理谁」。"""
    db = FakeDB([
        FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]}),
        FakeAccount(2, "u2", {"tasks": [T("create_canvas", "completed")]}),
    ])
    seen = []
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(
                            next(a for a in db.accounts if a.auth_json == auth_json).scenario))
    gt.run_growth_tasks(db, on_step=seen.append)
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# P1：互斥（手动补跑 vs 定时调度）
# ---------------------------------------------------------------------------

def test_run_lock_blocks_concurrent_run(monkeypatch):
    """锁被占用时必须立刻让路，而不是排队等待（排队会挂住 HTTP 请求）。"""
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("create_canvas", "completed")]})])
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(db.accounts[0].scenario))
    assert gt._RUN_LOCK.acquire(blocking=False)
    try:
        res = gt.run_growth_tasks(db)
        assert "已有成长任务在执行" in res.get("skipped", "")
        assert db.commits == 0        # 一步都没跑
    finally:
        gt._RUN_LOCK.release()
    # 释放后应能正常跑
    res2 = gt.run_growth_tasks(db)
    assert res2["claimed"] == 1


def test_lock_released_even_on_failure(monkeypatch):
    """异常路径也必须释放锁，否则后续所有轮次都被永久挡掉。"""
    db = FakeDB([FakeAccount(1, "u1", {"tasks": "raise"})])
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(db.accounts[0].scenario))
    gt.run_growth_tasks(db)                       # 单号抛异常
    assert not gt._RUN_LOCK.locked()              # 锁已归还
    assert gt.run_growth_tasks(db)["accounts_total"] == 1


# ---------------------------------------------------------------------------
# P1：只读任务清单 describe_account_tasks
# ---------------------------------------------------------------------------

def test_describe_account_tasks_classifies(monkeypatch):
    """分类必须与编排分支一致，否则前端会显示「可补跑」但补跑什么都不做。"""
    acc = FakeAccount(1, "u1", {"tasks": [
        T("create_canvas", "completed"),            # → claimable
        T("skill_1", "accepted", cur=0, tgt=1),     # → actionable
        T("chat_5", "claimed"),                     # → done
        T("Expert_Philanthropy", "accepted"),       # → manual（不可伪造）
        T("Buddy_App", "accepted"),                 # → manual（无事件分支）
    ]})
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(acc.scenario))
    # 固定为白天，让 black_cat 语义确定（本用例未含它）
    monkeypatch.setattr(es, "in_night_window", lambda now_utc=None: False)
    d = gt.describe_account_tasks(acc)
    by = {t["code"]: t["action"] for t in d["tasks"]}
    assert by["create_canvas"] == "claimable"
    assert by["skill_1"] == "actionable"
    assert by["chat_5"] == "done"
    assert by["Expert_Philanthropy"] == "manual"
    assert by["Buddy_App"] == "manual"
    s = d["summary"]
    assert s["total"] == 5 and s["claimable"] == 1 and s["actionable"] == 1
    assert s["manual"] == 2 and s["claimed"] == 1


def test_describe_account_tasks_is_readonly(monkeypatch):
    """只读：绝不 accept / report / claim。"""
    acc = FakeAccount(1, "u1", {"tasks": [T("create_canvas", "not_accepted")]})
    made = []

    def fake_session(auth_json):
        sess = FakeSess(acc.scenario)
        made.append(sess)
        return sess

    monkeypatch.setattr(gt, "AccountSession", fake_session)
    gt.describe_account_tasks(acc)
    calls = made[0].calls
    assert calls == ["fetch_tasks"]      # 只有一次拉取


def test_describe_account_tasks_night_pending(monkeypatch):
    """夜猫子在白天应标为 night（补跑也点不亮），夜间则归为可推进。"""
    acc = FakeAccount(1, "u1", {"tasks": [T("black_cat", "accepted", cur=0, tgt=3)]})
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(acc.scenario))

    monkeypatch.setattr(es, "in_night_window", lambda now_utc=None: False)
    d = gt.describe_account_tasks(acc)
    assert d["tasks"][0]["action"] == "night"
    assert d["summary"]["night_pending"] == 1

    monkeypatch.setattr(es, "in_night_window", lambda now_utc=None: True)
    d2 = gt.describe_account_tasks(acc)
    assert d2["tasks"][0]["action"] == "actionable"


def test_describe_account_tasks_handles_fetch_error(monkeypatch):
    """上游拉取失败时返回 errors 而不是抛异常（路由据此回 502）。"""
    acc = FakeAccount(1, "u1", {"tasks": "raise"})
    monkeypatch.setattr(gt, "AccountSession",
                        lambda auth_json: FakeSess(acc.scenario))
    d = gt.describe_account_tasks(acc)
    assert d["errors"] and d["tasks"] == []
