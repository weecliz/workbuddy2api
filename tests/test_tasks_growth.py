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
    db = FakeDB([FakeAccount(1, "u1", {"tasks": [T("black_cat", "accepted", cur=0, tgt=1)]})])
    res, made = run(db, monkeypatch)
    calls = made[0][1].calls
    assert not any(c.startswith("report_events") for c in calls)
    assert "claim_task:black_cat" not in calls
    assert any("待夜间窗口" in d for d in res["details"])


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
    db = FakeDB([FakeAccount(1, "u1", {
        "tasks": [T("create_canvas", "completed")], "claim_ok": False})])
    res, made = run(db, monkeypatch)
    assert res["claimed"] == 0 and res["failed"] == 0
    assert any("领奖待结算" in d for d in res["details"])


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
