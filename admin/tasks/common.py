"""猫猫旅行 / 活跃上报共享的风控参数与「领养门槛未达」当日防抖。

    ACCOUNT_DELAY          账号间限速（秒）
    ADOPT_THRESHOLD_MARKER 上游判定领养门槛未达的固定关键词

防抖 dict 常驻内存：进程重启即清零（与 Go 版一致，无需持久化）——
最坏情况只是当天多试一次领养，代价可接受，换不来持久化复杂度。
"""
from datetime import datetime, timedelta

ACCOUNT_DELAY = 0.8        # 全量账号约 0.8s/个，避免上游风控
ADOPT_THRESHOLD_MARKER = "first_buddy task not completed yet"

# uid → 上游自然日（CST）。同日不再重试领养，避免对上游重试轰炸。
_adopt_tried: dict[str, str] = {}


def upstream_today() -> str:
    """上游自然日（00:00 CST 重置）格式 YYYY-MM-DD。"""
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def adopt_tried_today(uid: str) -> bool:
    return _adopt_tried.get(uid) == upstream_today()


def mark_adopt_tried(uid: str) -> None:
    _adopt_tried[uid] = upstream_today()


def is_adopt_threshold_error(e: Exception) -> bool:
    """领养对话量门槛未达标：上游 HTTP 400 + 固定关键词，属预期行为。"""
    return ADOPT_THRESHOLD_MARKER in str(e).lower()
