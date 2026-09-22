"""会话亲和：亲和键派生 + 绑定表行为。

这些是纯函数/纯内存态，不碰数据库。
"""
from __future__ import annotations

import time

from admin.affinity import SessionAffinity, derive_affinity_key

MSGS = [{"role": "system", "content": "SYS"},
        {"role": "user", "content": "第一个问题"}]


# ---------------------------------------------------------------------------
# derive_affinity_key
# ---------------------------------------------------------------------------

def test_key_is_stable_for_same_conversation():
    assert derive_affinity_key(MSGS, scope=1) == derive_affinity_key(MSGS, scope=1)


def test_key_has_prefix():
    key = derive_affinity_key(MSGS, scope=1)
    assert key is not None and key.startswith("pfx-")


def test_different_conversations_get_different_keys():
    other = [{"role": "system", "content": "SYS"},
             {"role": "user", "content": "另一个问题"}]
    assert derive_affinity_key(other, scope=1) != derive_affinity_key(MSGS, scope=1)


def test_scope_isolates_tenants():
    """同一段前缀 + 不同租户 → 不同键（避免跨租户共绑同一账号）。"""
    assert derive_affinity_key(MSGS, scope=1) != derive_affinity_key(MSGS, scope=2)


def test_key_ignores_later_turns():
    """只取前两条：后续轮次增长不会改变亲和键（否则每轮都会换号）。"""
    base = derive_affinity_key(MSGS, scope=1)
    longer = MSGS + [{"role": "assistant", "content": "答"},
                     {"role": "user", "content": "追问"}]
    assert derive_affinity_key(longer, scope=1) == base


def test_key_only_first_two_messages_matter():
    """第三条之后的内容变化不影响键。"""
    a = MSGS + [{"role": "assistant", "content": "A"}]
    b = MSGS + [{"role": "assistant", "content": "B"}]
    assert derive_affinity_key(a, scope=1) == derive_affinity_key(b, scope=1)


def test_empty_inputs_return_none():
    assert derive_affinity_key([], scope=1) is None
    assert derive_affinity_key(None, scope=1) is None


def test_unserializable_content_returns_none_not_raise():
    """消息里若有不可 JSON 序列化的对象，退化为「无亲和」而不是让请求失败。"""
    class Weird:
        pass

    assert derive_affinity_key([{"role": "user", "content": Weird()}], scope=1) is None


def test_key_without_scope_works():
    k = derive_affinity_key(MSGS)
    assert k is not None and k.startswith("pfx-")


# ---------------------------------------------------------------------------
# SessionAffinity
# ---------------------------------------------------------------------------

def test_bind_get_unbind_roundtrip():
    aff = SessionAffinity(ttl=10, max_entries=10)
    assert aff.get("k") is None
    aff.bind("k", 7)
    assert aff.get("k") == 7
    aff.unbind("k")
    assert aff.get("k") is None


def test_empty_keys_are_noops():
    aff = SessionAffinity(ttl=10, max_entries=10)
    aff.bind("", 1)
    aff.bind(None, 1)
    aff.bind("k", None)
    aff.unbind(None)
    aff.unbind("")
    assert aff.get("") is None
    assert aff.get(None) is None


def test_expiry():
    aff = SessionAffinity(ttl=0.05, max_entries=10)
    aff.bind("k", 1)
    assert aff.get("k") == 1
    time.sleep(0.08)
    assert aff.get("k") is None


def test_get_renews_ttl():
    """滑动续期：持续访问的对话不会被过期掉。"""
    aff = SessionAffinity(ttl=0.12, max_entries=10)
    aff.bind("k", 1)
    for _ in range(3):
        time.sleep(0.07)
        assert aff.get("k") == 1


def test_max_entries_is_enforced():
    aff = SessionAffinity(ttl=100, max_entries=3)
    for i in range(10):
        aff.bind(f"k{i}", i)
    assert aff.size() <= 3


def test_rebind_overwrites():
    aff = SessionAffinity(ttl=10, max_entries=10)
    aff.bind("k", 1)
    aff.bind("k", 2)
    assert aff.get("k") == 2


def test_clear():
    aff = SessionAffinity(ttl=10, max_entries=10)
    aff.bind("k", 1)
    aff.clear()
    assert aff.get("k") is None
