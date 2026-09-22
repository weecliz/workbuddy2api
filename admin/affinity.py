"""会话亲和：把同一对话的连续轮次固定到同一账号。

为什么需要
----------
上游的前缀缓存（prompt cache）**按账号隔离**。本网关的选号策略是
「剩余最多优先」/「最久未用优先」，随账号余额与使用时间不断变化 ——
同一条对话的上一轮在账号 A、下一轮可能落到账号 B，B 侧没有任何缓存，
每一轮都要重新处理整个前缀（长 harness 会话动辄上万 token）。

做法
----
把「对话的稳定前缀」哈希成亲和键，键 → 账号 id 建立短期绑定：

  - 对话开头两条消息（system + 首轮 user）在整个对话生命周期内逐字节不变，
    用它做哈希即可稳定标识一条对话；不同对话因首轮内容不同而自然分散。
  - 绑定账号仍不可用（冷却 / 余额耗尽 / 本轮已试过）时解绑，走常规选号后
    重新绑定，保证请求始终能发出去 —— 亲和只是优化，不是可用性依赖。

多进程说明
----------
绑定表是**进程内**的（与参考实现 hub 的 SessionAffinity 一致）。若以多 worker
方式部署，每个 worker 各持一份表，同一对话可能落到不同账号 —— 亲和效果打折
但不会出错。如需跨进程共享，后续可仿 admin/ratelimit.py 增加 Redis 后端。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Optional


class SessionAffinity:
    """线程安全的「亲和键 → 账号 id」短期绑定表（滑动 TTL + 容量上限）。"""

    def __init__(self, ttl: float = 7200.0, max_entries: int = 5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._bindings: dict[str, tuple[int, float]] = {}

    def get(self, key: Optional[str]) -> Optional[int]:
        """取绑定并续期（滑动 TTL）。未绑定或已过期返回 None。"""
        if not key:
            return None
        now = time.time()
        with self._lock:
            entry = self._bindings.get(key)
            if not entry:
                return None
            acc_id, expires = entry
            if now > expires:
                self._bindings.pop(key, None)
                return None
            self._bindings[key] = (acc_id, now + self.ttl)
            return acc_id

    def bind(self, key: Optional[str], acc_id: Optional[int]) -> None:
        """建立/覆盖绑定。超出容量时先清过期项，仍超限则按到期时间丢最旧的一批。"""
        if not key or acc_id is None:
            return
        now = time.time()
        with self._lock:
            if len(self._bindings) >= self.max_entries:
                self._bindings = {k: v for k, v in self._bindings.items() if v[1] > now}
                overflow = len(self._bindings) - self.max_entries + 1
                if overflow > 0:
                    oldest = sorted(self._bindings.items(), key=lambda kv: kv[1][1])[:overflow]
                    for k, _ in oldest:
                        self._bindings.pop(k, None)
            self._bindings[key] = (acc_id, now + self.ttl)

    def unbind(self, key: Optional[str]) -> None:
        if not key:
            return
        with self._lock:
            self._bindings.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def size(self) -> int:
        """当前绑定条数（含尚未被清理的过期项）。"""
        with self._lock:
            return len(self._bindings)


def derive_affinity_key(messages, scope=None) -> Optional[str]:
    """从对话的稳定前缀派生亲和键；无法派生时返回 None（调用方按无亲和处理）。

    取前两条消息（system + 首轮 user）—— 它们在整条对话内逐字节不变，
    正是上游前缀缓存赖以命中的那一段。不同对话因首轮内容不同而自然分散。

    scope 用于多租户隔离（传 API Key 的 id）：不同租户即使首轮内容巧合相同，
    也不会被绑到同一账号上。
    """
    try:
        msgs = messages or []
        if not msgs:
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if scope is not None:
            blob += b"|" + str(scope).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        # 消息里若有不可 JSON 序列化的对象，退化为「无亲和」而不是让请求失败
        return None


# 进程内单例：由 admin/config.py 的 ACCOUNT_AFFINITY_* 决定是否启用与参数。
# 延迟构造，避免导入期就读取 settings（便于测试替换）。
_INSTANCE: Optional[SessionAffinity] = None
_INSTANCE_LOCK = threading.Lock()


def get_affinity() -> SessionAffinity:
    """取进程内单例（首次调用时按 settings 构造）。"""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                from admin.config import settings

                _INSTANCE = SessionAffinity(
                    ttl=getattr(settings, "ACCOUNT_AFFINITY_TTL", 7200),
                    max_entries=getattr(settings, "ACCOUNT_AFFINITY_MAX", 5000),
                )
    return _INSTANCE
