"""Anthropic 模型名映射规则（`core/anthropic_model_map.py` + 两侧调用点）。

背景：Claude Code / CC Switch 发来的是 `claude-opus-4-6` 这类上游不认识的名字，
需要按 `.env` 配置翻译。规则原先只有「opus / sonnet / haiku 三档」，本次补上
「精确映射表」并把两侧（`/v1/messages` 与 `/gw/v1/messages`）统一到同一套判定。

覆盖点：
  - 纯函数：解析（含非法条目收集）、`[1m]` 规范化、精确查表、档次匹配顺序
  - admin `/v1/messages`：精确映射 > 白名单 > 档次 > auto 的完整优先级
  - converter `/gw/v1/messages`：同一套规则，但未命中保持原样透传（无白名单概念）
  - converter 的懒加载：未注入配置时从环境变量读，且只读一次
"""
from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

import admin.routers.proxy as proxy
import core.converter as converter
from admin.config import settings
from core.anthropic_model_map import (
    load_tiers_from_env,
    lookup_exact,
    match_tier,
    normalize_lookup_key,
    parse_model_map,
)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_parse_model_map_parses_and_trims():
    mapping, invalid = parse_model_map(
        " claude-opus-4-6 = glm-5.3 ,, claude-sonnet-4-5=deepseek-v4.1-flash "
    )
    assert mapping == {
        "claude-opus-4-6": "glm-5.3",
        "claude-sonnet-4-5": "deepseek-v4.1-flash",
    }
    assert invalid == []


def test_parse_model_map_collects_invalid_entries():
    """一个手滑的条目不该让整个服务起不来，只收集起来供调用方告警。"""
    mapping, invalid = parse_model_map("claude-opus-4-6, =glm-5.3, claude-x=")
    assert mapping == {}
    assert invalid == ["claude-opus-4-6", "=glm-5.3", "claude-x="]


def test_parse_model_map_empty_input():
    assert parse_model_map("") == ({}, [])
    assert parse_model_map(None) == ({}, [])


def test_normalize_lookup_key_strips_context_suffix():
    assert normalize_lookup_key("  Claude-Opus-4-6[1M] ") == "claude-opus-4-6"
    assert normalize_lookup_key("GLM-5.3") == "glm-5.3"


def test_lookup_exact_case_insensitive_and_1m_compatible():
    mapping = {"claude-opus-4-6": "glm-5.3"}
    assert lookup_exact("CLAUDE-OPUS-4-6", mapping) == "glm-5.3"
    # Claude Code 生态里常照抄带 [1m] 的写法，必须同样命中
    assert lookup_exact("claude-opus-4-6[1m]", mapping) == "glm-5.3"
    assert lookup_exact("claude-opus-4-7", mapping) is None


def test_match_tier_prefers_opus_then_sonnet_then_haiku():
    tiers = {"opus": "o", "sonnet": "s", "haiku": "h"}
    assert match_tier("claude-opus-4-6", tiers) == "o"
    assert match_tier("claude-sonnet-4-5", tiers) == "s"
    assert match_tier("claude-haiku-4-5", tiers) == "h"
    # 同时含两个档次词时按 opus → sonnet → haiku 顺序取第一个
    assert match_tier("claude-opus-sonnet", tiers) == "o"
    assert match_tier("glm-5.3", tiers) is None


def test_match_tier_skips_empty_targets():
    """某档未配置（空串）时视为不参与匹配，而不是映射成空模型名。"""
    assert match_tier("claude-haiku-4-5", {"opus": "o", "haiku": ""}) is None


def test_load_tiers_from_env(monkeypatch):
    monkeypatch.setenv("ADMIN_ANTHROPIC_MODEL_OPUS", "glm-5.3")
    monkeypatch.setenv("ADMIN_ANTHROPIC_MODEL_SONNET", "  deepseek-v4.1-flash  ")
    monkeypatch.delenv("ADMIN_ANTHROPIC_MODEL_HAIKU", raising=False)
    assert load_tiers_from_env() == {
        "opus": "glm-5.3",
        "sonnet": "deepseek-v4.1-flash",
        "haiku": "",
    }


# ---------------------------------------------------------------------------
# admin /v1/messages：_map_anthropic_model
# ---------------------------------------------------------------------------

class _ModelRow:
    def __init__(self, model_id, enabled=1):
        self.model_id = model_id
        self.enabled = enabled


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _WhitelistDB:
    """只实现 _pick_best_model / _is_model_allowed 需要的 query().all()/.first()。"""

    def __init__(self, model_ids=()):
        self._rows = [_ModelRow(m) for m in model_ids]

    def query(self, _model):
        return _Query(self._rows)


def _db(*model_ids: str) -> Session:
    """白名单替身：假装成 Session，实际不连任何库（cast 只表意图）。"""
    return cast(Session, _WhitelistDB(model_ids))


@pytest.fixture
def tiers(monkeypatch):
    """固定三档目标，避免测试受本机 .env 影响。"""
    monkeypatch.setattr(proxy, "_ANTHROPIC_MODEL_TIERS", {
        "opus": "deepseek-v4-pro",
        "sonnet": "glm-5.2",
        "haiku": "glm-5.3-flash",
    })


@pytest.fixture
def exact_map(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_MODEL_MAP", {
        "claude-opus-4-6": "glm-5.3",
    }, raising=False)


def test_admin_exact_map_wins_over_whitelist(tiers, exact_map):
    """精确映射优先于「名字已在白名单里就原样透传」——映射表是显式配置。"""
    db = _db("claude-opus-4-6", "glm-5.3")
    assert proxy._map_anthropic_model(db, "claude-opus-4-6") == "glm-5.3"


def test_admin_exact_map_is_case_insensitive(tiers, exact_map):
    db = _db("glm-5.3")
    assert proxy._map_anthropic_model(db, "Claude-Opus-4-6[1M]") == "glm-5.3"


def test_admin_whitelist_passthrough(tiers, exact_map):
    """白名单里的名字原样透传，档次映射不参与。"""
    db = _db("glm-5.3")
    assert proxy._map_anthropic_model(db, "glm-5.3") == "glm-5.3"


def test_admin_tier_fallback(tiers, exact_map):
    db = _db("glm-5.3")
    assert proxy._map_anthropic_model(db, "claude-opus-4-7") == "deepseek-v4-pro"
    assert proxy._map_anthropic_model(db, "claude-sonnet-4-5") == "glm-5.2"
    assert proxy._map_anthropic_model(db, "claude-haiku-4-5") == "glm-5.3-flash"


def test_admin_unknown_model_falls_to_auto(tiers, exact_map):
    """档次词都没命中的 claude-* 仍落 auto（历史行为，避免瞎猜模型）。"""
    db = _db("glm-5.3")
    assert proxy._map_anthropic_model(db, "claude-3-5-turbo") == "auto"


def test_admin_auto_and_empty_stay_auto(tiers, exact_map):
    db = _db("glm-5.3")
    assert proxy._map_anthropic_model(db, "auto") == "auto"
    assert proxy._map_anthropic_model(db, "") == "auto"
    assert proxy._map_anthropic_model(db, "   ") == "auto"


# ---------------------------------------------------------------------------
# converter /gw/v1/messages：_anthropic_model_stage
# ---------------------------------------------------------------------------

@pytest.fixture
def conv_cfg(monkeypatch):
    """直接注入已加载的配置（等价于 admin 挂载 /gw 时的注入）。"""
    monkeypatch.setitem(converter.CONFIG, "model_map", {"claude-opus-4-6": "glm-5.3"})
    monkeypatch.setitem(converter.CONFIG, "model_tiers", {
        "opus": "deepseek-v4-pro",
        "sonnet": "glm-5.2",
        "haiku": "glm-5.3-flash",
    })


def test_converter_exact_map_then_tier(conv_cfg):
    assert converter._anthropic_model_stage("claude-opus-4-6") == "glm-5.3"
    assert converter._anthropic_model_stage("claude-opus-4-7") == "deepseek-v4-pro"
    assert converter._anthropic_model_stage("claude-haiku-4-5") == "glm-5.3-flash"


def test_converter_unknown_name_passes_through(conv_cfg):
    """本端点没有白名单，未命中的名字必须原样透传（改动前的行为）。"""
    assert converter._anthropic_model_stage("glm-5.3") == "glm-5.3"
    assert converter._anthropic_model_stage("some-unknown-model") == "some-unknown-model"


def test_converter_auto_and_empty(conv_cfg):
    assert converter._anthropic_model_stage("") == "auto"
    assert converter._anthropic_model_stage("auto") == "auto"


def test_converter_lazy_loads_env_once(monkeypatch):
    """未注入配置时从环境变量读，并缓存 —— 与后台同一组变量名。"""
    monkeypatch.setitem(converter.CONFIG, "model_map", None)
    monkeypatch.setitem(converter.CONFIG, "model_tiers", None)
    monkeypatch.setenv("ADMIN_ANTHROPIC_MODEL_MAP", "claude-opus-4-6=glm-5.3")
    monkeypatch.setenv("ADMIN_ANTHROPIC_MODEL_SONNET", "deepseek-v4.1-flash")

    assert converter._anthropic_model_stage("claude-opus-4-6") == "glm-5.3"
    assert converter._anthropic_model_stage("claude-sonnet-4-5") == "deepseek-v4.1-flash"
    assert converter.CONFIG["model_map"] == {"claude-opus-4-6": "glm-5.3"}
    assert converter.CONFIG["model_tiers"]["sonnet"] == "deepseek-v4.1-flash"
