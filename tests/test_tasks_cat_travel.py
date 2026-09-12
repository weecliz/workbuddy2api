"""猫猫旅行任务的状态机测试（任务实现移到 admin/tasks/ 后的回归）。

覆盖 travelOne 状态机的每个分支，以及「领养门槛未达」的当日防抖：
  无猫 → 领养成功 / 门槛未达（打防抖标记，同日二次调用直接跳过）
  有猫 → arrived 领奖 / idle 派出 / idle 但名额用完 / traveling 跳过 / 未知状态
  异常 → 只记失败不中断其余账号
"""
import pytest

from admin.tasks import cat_travel as ct
from admin.tasks import common


class FakeSess:
    """按 scenario 回放上游响应；记录调用便于断言。"""

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.calls: list[str] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True

    def updated_json(self):
        return "{}"

    def buddy_info(self):
        self.calls.append("buddy_info")
        return self.scenario.get("buddy")

    def buddy_agreement(self):
        self.calls.append("buddy_agreement")

    def buddy_first(self):
        self.calls.append("buddy_first")
        err = self.scenario.get("adopt_error")
        if err:
            raise RuntimeError(err)
        return {}

    def travel_status(self):
        self.calls.append("travel_status")
        return self.scenario.get("travel", {})

    def travel_depart(self, location_id):
        self.calls.append(f"travel_depart:{location_id}")

    def travel_claim(self, record_id):
        self.calls.append(f"travel_claim:{record_id}")
        return self.scenario.get("reward", 0)


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
    monkeypatch.setattr(ct.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(common, "_adopt_tried", {})


def run(db, monkeypatch):
    """用假会话执行任务：账号的 scenario 直接挂在其 auth_json 上。"""
    made = []

    def fake_session(auth_json):
        acc = next(a for a in db.accounts if a.auth_json == auth_json)
        sess = FakeSess(acc.scenario)
        made.append((acc, sess))
        return sess

    monkeypatch.setattr(ct, "AccountSession", fake_session)
    return ct.run_cat_travel(db), made


def test_adopt_success(monkeypatch):
    db = FakeDB([FakeAccount(1, "u1", {"buddy": None})])
    res, made = run(db, monkeypatch)
    assert res["adopted"] == 1 and res["skipped"] == 0
    assert made[0][1].calls == ["buddy_info", "buddy_agreement", "buddy_first"]
    assert "领养成功(+300)" in res["details"][0]


def test_adopt_threshold_marks_debounce(monkeypatch):
    scenario = {"buddy": None, "adopt_error": "first_buddy task not completed yet"}
    db = FakeDB([FakeAccount(7, "u-th", scenario)])
    res, _ = run(db, monkeypatch)
    assert res["skipped"] == 1 and res["adopted"] == 0
    assert "领养门槛未达" in res["details"][0]
    assert common.adopt_tried_today("u-th") is True
    # 同日再次执行：防抖直接跳过，不再请求领养
    res2, made2 = run(db, monkeypatch)
    assert res2["skipped"] == 1
    assert made2[0][1].calls == ["buddy_info"]
    assert "防抖跳过" in res2["details"][0]


def test_claim_when_arrived(monkeypatch):
    db = FakeDB([FakeAccount(2, "u2", {
        "buddy": {"name": "量子喵"},
        "travel": {"state": "arrived", "record_id": 4593494},
        "reward": 120,
    })])
    res, made = run(db, monkeypatch)
    assert res["claimed"] == 1
    assert "travel_claim:4593494" in made[0][1].calls
    assert "领奖+120(量子喵)" in res["details"][0]


def test_arrived_without_record_id_skips(monkeypatch):
    db = FakeDB([FakeAccount(3, "u3", {
        "buddy": {"name": "设计喵"},
        "travel": {"state": "arrived", "record_id": 0},
    })])
    res, made = run(db, monkeypatch)
    assert res["skipped"] == 1 and res["claimed"] == 0
    assert "travel_claim" not in "".join(made[0][1].calls)
    assert "缺record_id" in res["details"][0]


def test_depart_when_idle(monkeypatch):
    db = FakeDB([FakeAccount(4, "u4", {
        "buddy": {"name": "斜杠喵"},
        "travel": {"state": "idle", "daily_limit_reached": False},
    })])
    res, made = run(db, monkeypatch)
    assert res["departed"] == 1
    assert f"travel_depart:{ct.TRAVEL_LOCATION_ID}" in made[0][1].calls
    assert f"已派出(地点{ct.TRAVEL_LOCATION_ID},斜杠喵)" in res["details"][0]


def test_idle_daily_limit_reached_skips(monkeypatch):
    db = FakeDB([FakeAccount(5, "u5", {
        "buddy": {"name": "摸鱼喵"},
        "travel": {"state": "idle", "daily_limit_reached": True},
    })])
    res, made = run(db, monkeypatch)
    assert res["skipped"] == 1 and res["departed"] == 0
    assert "travel_depart" not in "".join(made[0][1].calls)
    assert "今日名额已用完" in res["details"][0]


def test_traveling_skips_with_eta(monkeypatch):
    import time
    arrive = int(time.time() * 1000) + 2 * 3600 * 1000
    db = FakeDB([FakeAccount(6, "u6", {
        "buddy": {"name": "旅行喵"},
        "travel": {"state": "traveling", "record_id": 999,
                   "arrive_at": arrive, "server_now": int(time.time() * 1000)},
    })])
    res, _ = run(db, monkeypatch)
    assert res["skipped"] == 1
    assert "旅行中(旅行喵" in res["details"][0]
    assert "小时后回" in res["details"][0]
    assert "record=999" in res["details"][0]


def test_unknown_state_and_exception(monkeypatch):
    db = FakeDB([
        FakeAccount(8, "u8", {"buddy": {"name": "x"}, "travel": {"state": "weird"}}),
        FakeAccount(9, "u9", {"buddy_error": True}),
    ])

    def fake_session(auth_json):
        acc = next(a for a in db.accounts if a.auth_json == auth_json)
        if acc.scenario.get("buddy_error"):
            raise RuntimeError("上游炸了")
        return FakeSess(acc.scenario)

    monkeypatch.setattr(ct, "AccountSession", fake_session)
    res = ct.run_cat_travel(db)
    assert res["skipped"] == 1 and res["failed"] == 1
    assert any("未知状态" in d for d in res["details"])
    assert any("失败:上游炸了" in d for d in res["details"])
    assert any("上游炸了" in e for e in res["errors"])
    assert db.commits >= 1  # token 写回后提交


def test_fmt_travel_eta_edge_cases():
    assert ct.fmt_travel_eta({}) == ""
    assert ct.fmt_travel_eta({"arrive_at": "bad"}) == ""
    now = 1_700_000_000_000
    assert ct.fmt_travel_eta({"arrive_at": now - 1000, "server_now": now}) == ",即将到站"
    assert ct.fmt_travel_eta({"arrive_at": now + 30 * 60 * 1000, "server_now": now}) == ",约30分钟后回"
    assert ct.fmt_travel_eta({"arrive_at": now + 5 * 3600 * 1000, "server_now": now}) == ",约5小时后回"
