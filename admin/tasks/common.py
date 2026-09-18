"""猫猫旅行 / 活跃上报共享的风控参数与「领养门槛未达」当日防抖。

    ACCOUNT_DELAY          账号间限速（秒）
    ADOPT_THRESHOLD_MARKER 上游判定领养门槛未达的固定关键词

防抖 dict 常驻内存：进程重启即清零（与 Go 版一致，无需持久化）——
最坏情况只是当天多试一次领养，代价可接受，换不来持久化复杂度。
"""
from datetime import datetime, timedelta
import random

ACCOUNT_DELAY = 0.8        # 全量账号约 0.8s/个，避免上游风控


def jitter_delay(min_s: float, max_s: float) -> float:
    """在 [min_s, max_s) 区间均匀取一个秒数，给账号间限速加抖动。

    固定间隔的批量请求在服务端日志里是等间距的机器形态；带随机抖动后
    看起来更像分散的用户行为。取值区间由调用方给出。
    """
    return random.uniform(min_s, max_s)
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
