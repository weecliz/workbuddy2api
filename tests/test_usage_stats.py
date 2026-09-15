"""用量统计的纯函数回归测试（不碰网络）。

覆盖 admin/backend/usage.py 的三个关键契约：
  1. aggregate_records —— 区间夹取、按天/按模型分桶、异常字段不炸；
  2. merge_aggregates  —— 多账号汇总的加总与时间边界；
  3. fetch_usage_range —— 分页翻页/停止/截断判定；
  4. normalize_range   —— 区间不写死当天、只给日期补时分秒、颠倒自动交换。

这些函数是「页面数字对不对」的唯一依据，故与上游 HTTP 解耦后单独测。
"""
from datetime import datetime

from admin.backend import usage as U


def _rec(ts, credit, model="m1", client="WorkBuddy", purpose="conversation", rid="r"):
    return {"requestTime": ts, "credit": credit, "model": model, "client": client,
            "agentPurpose": purpose, "requestId": rid, "inputTrunc": "hi"}


# --------------------------------------------------------------------------
# aggregate_records
# --------------------------------------------------------------------------

def test_aggregate_buckets_by_day_and_model():
    recs = [
        _rec("2026-09-15 10:00:00", 1.5, "glm-5.3"),
        _rec("2026-09-15 11:00:00", 0.5, "glm-5.3"),
        _rec("2026-09-14 09:00:00", 2.0, "deepseek-v4.1-flash"),
    ]
    out = U.aggregate_records(recs)
    assert out["summary"]["requests"] == 3
    assert out["summary"]["credits"] == 4.0
    # 按天升序，且每天只统计自己的量
    assert [d["date"] for d in out["by_day"]] == ["2026-09-14", "2026-09-15"]
    assert out["by_day"][1]["requests"] == 2
    assert out["by_day"][1]["credits"] == 2.0
    # 按模型按积分降序；积分并列时保持插入序（稳定排序）
    assert out["by_model"][0]["credits"] == 2.0
    assert {m["model"]: m["credits"] for m in out["by_model"]} == {
        "glm-5.3": 2.0, "deepseek-v4.1-flash": 2.0}
    assert out["earliest_request_at"] == "2026-09-14 09:00:00"
    assert out["latest_request_at"] == "2026-09-15 11:00:00"


def test_aggregate_clamps_to_requested_range():
    """上游按账号时区返回，边界可能越界；本地必须再夹一次，保证与按天分桶自洽。"""
    recs = [
        _rec("2026-09-13 23:59:59", 9.0),   # 早于 start，应被剔除
        _rec("2026-09-14 12:00:00", 1.0),
        _rec("2026-09-16 00:00:01", 9.0),   # 晚于 end，应被剔除
    ]
    out = U.aggregate_records(recs,
                              start=datetime(2026, 9, 14, 0, 0, 0),
                              end=datetime(2026, 9, 15, 23, 59, 59))
    assert out["summary"]["requests"] == 1
    assert out["summary"]["credits"] == 1.0
    assert [d["date"] for d in out["by_day"]] == ["2026-09-14"]


def test_aggregate_tolerates_bad_fields():
    """上游字段是外部数据：credit 非法、时间不可解析、字段缺失都不能让统计炸。"""
    recs = [
        {"requestTime": "2026-09-15 10:00:00", "credit": "abc", "model": None},
        {"requestTime": "not-a-time", "credit": None},
        {"credit": 0.5},
    ]
    out = U.aggregate_records(recs)
    assert out["summary"]["requests"] == 3
    assert out["summary"]["credits"] == 0.5
    # 时间不可解析的记录仍计入总量，但不进按天分桶（否则会被塞进错误日期）
    assert [d["date"] for d in out["by_day"]] == ["2026-09-15"]
    # 三条记录的 model 都缺失/为 None，统一归入「未知」
    assert out["summary"]["models"] == {"未知": 3}


def test_aggregate_empty():
    out = U.aggregate_records([])
    assert out["summary"]["requests"] == 0
    assert out["summary"]["credits"] == 0.0
    assert out["by_day"] == [] and out["by_model"] == []


# --------------------------------------------------------------------------
# merge_aggregates
# --------------------------------------------------------------------------

def test_merge_adds_up_across_accounts():
    a = U.aggregate_records([_rec("2026-09-15 10:00:00", 1.0, "glm-5.3")])
    b = U.aggregate_records([_rec("2026-09-15 12:00:00", 2.0, "glm-5.3"),
                             _rec("2026-09-15 13:00:00", 3.0, "kimi-k2.7")])
    m = U.merge_aggregates([a, b])
    assert m["summary"]["requests"] == 3
    assert m["summary"]["credits"] == 6.0
    # 同一天合并成一条，同模型合并成一条
    assert len(m["by_day"]) == 1 and m["by_day"][0]["requests"] == 3
    models = {x["model"]: x["credits"] for x in m["by_model"]}
    assert models == {"kimi-k2.7": 3.0, "glm-5.3": 3.0}
    # 时间边界取全局最早 / 最晚
    assert m["earliest_request_at"] == "2026-09-15 10:00:00"
    assert m["latest_request_at"] == "2026-09-15 13:00:00"


def test_merge_empty_list_is_safe():
    m = U.merge_aggregates([])
    assert m["summary"]["requests"] == 0
    assert m["summary"]["credits"] == 0.0
    assert m["by_day"] == [] and m["by_model"] == []


# --------------------------------------------------------------------------
# fetch_usage_range（用假会话驱动翻页，不打网络）
# --------------------------------------------------------------------------

class FakeSess:
    """按 pageSize 切片回放，模拟上游 total + 降序分页语义。"""

    def __init__(self, total, page_size_limit=None):
        self.total = total
        self.page_size_limit = page_size_limit
        self.calls: list[tuple[int, int]] = []

    def fetch_request_usage(self, start, end, page, page_size):
        self.calls.append((page, page_size))
        # 模拟上游：一次最多返回 page_size_limit 条
        cap = min(page_size, self.page_size_limit or page_size)
        start_idx = (page - 1) * page_size
        n = max(0, min(cap, self.total - start_idx))
        rows = [_rec(f"2026-09-15 10:{i:02d}:00", 0.1, rid=f"r{start_idx + i}")
                for i in range(n)]
        return {"code": 0, "data": {"total": self.total, "data": rows}}


def test_fetch_usage_range_pages_until_total():
    sess = FakeSess(total=250)
    out = U.fetch_usage_range(sess, "s", "e", page_size=100)
    assert out["total"] == 250
    assert out["pages"] == 3
    assert len(out["records"]) == 250
    assert out["truncated"] is False
    assert sess.calls == [(1, 100), (2, 100), (3, 100)]


def test_fetch_usage_range_single_short_page_stops():
    """首屏不足一页就应立刻停止，不能继续空翻。"""
    sess = FakeSess(total=7)
    out = U.fetch_usage_range(sess, "s", "e", page_size=500)
    assert len(out["records"]) == 7
    assert out["pages"] == 1
    assert sess.calls == [(1, 500)]


def test_fetch_usage_range_marks_truncated_at_page_cap():
    """达到 max_pages 仍未拉完 → truncated=True，供前端提示而非静默少算。"""
    sess = FakeSess(total=1000)
    out = U.fetch_usage_range(sess, "s", "e", page_size=50, max_pages=3)
    assert out["pages"] == 3
    assert len(out["records"]) == 150
    assert out["truncated"] is True


def test_fetch_usage_range_handles_empty():
    sess = FakeSess(total=0)
    out = U.fetch_usage_range(sess, "s", "e", page_size=100)
    assert out["records"] == []
    assert out["total"] == 0
    assert out["truncated"] is False


# --------------------------------------------------------------------------
# normalize_range（「不写死当天」的核心）
# --------------------------------------------------------------------------

def test_normalize_range_defaults_to_today():
    now = datetime(2026, 9, 15, 14, 30, 0)
    s, e, s_dt, e_dt = U.normalize_range("", "", now=now)
    assert s == "2026-09-15 00:00:00"
    assert e == "2026-09-15 23:59:59"
    assert s_dt.date() == e_dt.date() == now.date()


def test_normalize_range_accepts_arbitrary_dates():
    """核心诉求：必须是任意区间，而不是只能当日。"""
    s, e, s_dt, e_dt = U.normalize_range("2026-08-01", "2026-08-31")
    assert s == "2026-08-01 00:00:00"
    assert e == "2026-08-31 23:59:59"
    assert (e_dt - s_dt).days == 30


def test_normalize_range_accepts_full_timestamps():
    s, e, _, e_dt = U.normalize_range("2026-09-01 08:30:00", "2026-09-01 18:00:00")
    assert s == "2026-09-01 08:30:00"
    assert e == "2026-09-01 18:00:00"
    assert e_dt.hour == 18


def test_normalize_range_swaps_when_reversed():
    s, e, _, _ = U.normalize_range("2026-09-20", "2026-09-10")
    assert s == "2026-09-10 00:00:00"
    assert e == "2026-09-20 23:59:59"


def test_normalize_range_caps_absurd_span():
    """区间过大时截断起始日，避免一次查询翻上百页把上游拖死。"""
    s, e, s_dt, e_dt = U.normalize_range("2000-01-01", "2026-09-15")
    assert (e_dt - s_dt).days <= 366
    assert e == "2026-09-15 23:59:59"


def test_parse_ts_returns_none_on_garbage():
    assert U._parse_ts("") is None
    assert U._parse_ts("2026/09/15") is None
    assert U._parse_ts("2026-09-15 10:00:00") == datetime(2026, 9, 15, 10, 0, 0)
