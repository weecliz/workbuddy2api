"""思考开关翻译（S5）+ 档位归一（F13）。

覆盖上游实测的坑：
  - reasoning_effort="off"            → 上游 400 code=11150（声明了档位列表的模型）
  - reasoning_effort="none"/"minimal" → 上游**反而开启**思维链
  - enable_thinking / enableThinking  → 三方客户端的异名写法
"""
from __future__ import annotations

import importlib

import pytest

import core.converter as conv
from core.converter import (
    THINKING_OFF_SPELLINGS,
    inject_deepseek_reasoning,
    normalize_effort,
    prepare_outbound_body,
    wants_thinking,
)

DS = "deepseek-v4.1-flash"
GLM = "glm-5.3"


def _body(model, **extra):
    b = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    b.update(extra)
    return b


# ---------------------------------------------------------------------------
# wants_thinking：三态判定
# ---------------------------------------------------------------------------

def test_off_spellings_are_all_recognized_as_off():
    for spell in THINKING_OFF_SPELLINGS:
        assert wants_thinking({"reasoning_effort": spell}) is False, spell


def test_real_efforts_express_on():
    for eff in ("low", "medium", "high", "max"):
        assert wants_thinking({"reasoning_effort": eff}) is True, eff


def test_no_field_means_no_expression():
    """没表态必须返回 None —— 与「表态开启」严格区分。"""
    assert wants_thinking({}) is None


def test_thinking_object_shapes():
    assert wants_thinking({"thinking": {"type": "enabled"}}) is True
    assert wants_thinking({"thinking": {"type": "disabled"}}) is False
    # 只有 budget_tokens、没有 type：视为要思考
    assert wants_thinking({"thinking": {"budget_tokens": 100}}) is True


def test_thinking_bool_and_string_shapes():
    assert wants_thinking({"thinking": True}) is True
    assert wants_thinking({"thinking": False}) is False
    assert wants_thinking({"thinking": "enabled"}) is True
    assert wants_thinking({"thinking": "off"}) is False


def test_third_party_alias_spellings():
    """mirai-mifan 之类三方客户端用 enable_thinking / enableThinking。"""
    assert wants_thinking({"enable_thinking": True}) is True
    assert wants_thinking({"enable_thinking": False}) is False
    assert wants_thinking({"enableThinking": True}) is True


def test_reasoning_nested_summary_does_not_express_thinking():
    """{"summary":"auto"} 只说摘要，不是思考开关。"""
    assert wants_thinking({"reasoning": {"summary": "auto"}}) is None
    assert wants_thinking({"reasoning": {"effort": "high"}}) is True
    assert wants_thinking({"reasoning": {"effort": "off"}}) is False


# ---------------------------------------------------------------------------
# 关闭意图：绝不能把「关」变成「开」，也不能透传非法档位值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spell", sorted(THINKING_OFF_SPELLINGS))
def test_off_is_translated_not_passed_through(spell):
    out = inject_deepseek_reasoning(_body(DS, reasoning_effort=spell))
    # 必须写上游认得的开关
    assert out.get("thinking") == {"type": "disabled"}
    # 非法档位值绝不能残留（否则上游 400 或反向开启）
    assert out.get("reasoning_effort") is None
    assert out.get("reasoningEffort") is None


def test_off_via_thinking_object():
    out = inject_deepseek_reasoning(_body(DS, thinking={"type": "disabled"}))
    assert out.get("thinking") == {"type": "disabled"}
    assert out.get("reasoning_effort") is None


def test_off_via_alias():
    out = inject_deepseek_reasoning(_body(DS, enable_thinking=False))
    assert out.get("thinking") == {"type": "disabled"}
    assert out.get("reasoning_effort") is None


def test_off_strips_aliases_so_they_do_not_leak():
    out = inject_deepseek_reasoning(_body(DS, enable_thinking=False))
    assert "enable_thinking" not in out
    assert "enableThinking" not in out


def test_off_on_non_deepseek_still_strips_illegal_effort():
    """off/none 是协议级非法值：模型无关地摘掉，避免送到会校验的模型上 400。"""
    out = inject_deepseek_reasoning(_body(GLM, reasoning_effort="off"))
    assert out.get("reasoning_effort") is None


# ---------------------------------------------------------------------------
# 开启意图：异名/嵌套写法落地成扁平档位
# ---------------------------------------------------------------------------

def test_thinking_enabled_gets_flat_effort():
    out = inject_deepseek_reasoning(_body(DS, thinking={"type": "enabled"}))
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "high"


def test_nested_effort_is_lifted_to_flat_field():
    out = inject_deepseek_reasoning(
        _body(DS, thinking={"type": "enabled", "effort": "low"}))
    assert out["reasoning_effort"] == "low"


def test_alias_enable_thinking_is_translated():
    out = inject_deepseek_reasoning(_body(DS, enable_thinking=True))
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "high"


def test_explicit_effort_wins_over_default():
    out = inject_deepseek_reasoning(_body(DS, reasoning_effort="max"))
    assert out["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
# 没表态：由开关决定
# ---------------------------------------------------------------------------

def test_silent_request_defaults_on(monkeypatch):
    monkeypatch.delenv("ADMIN_DEEPSEEK_THINKING_DEFAULT", raising=False)
    out = inject_deepseek_reasoning(_body(DS))
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "high"


def test_silent_request_can_be_configured_off(monkeypatch):
    monkeypatch.setenv("ADMIN_DEEPSEEK_THINKING_DEFAULT", "0")
    out = inject_deepseek_reasoning(_body(DS))
    assert out.get("thinking") is None
    assert out.get("reasoning_effort") is None


def test_non_deepseek_is_never_touched_when_silent():
    out = inject_deepseek_reasoning(_body(GLM))
    assert out.get("thinking") is None
    assert out.get("reasoning_effort") is None


# ---------------------------------------------------------------------------
# 档位归一（F13）
# ---------------------------------------------------------------------------

def test_normalize_keeps_supported_effort():
    for eff in ("low", "high", "max"):
        assert normalize_effort(eff, DS) == eff


def test_normalize_rounds_down_to_supported():
    # 模型声明支持 [low, high, max]；medium 取「不高于它的最高支持档」= low
    assert normalize_effort("medium", DS) == "low"


def test_normalize_lifts_when_all_supported_are_higher():
    # minimal 低于所有支持档 → 取最低支持档（偏离最小）
    assert normalize_effort("minimal", DS) == "low"


def test_normalize_unknown_effort_falls_back_to_default():
    assert normalize_effort("weird", DS) == "high"


def test_normalize_passthrough_for_undeclared_model():
    """模型未声明支持列表 → 原样透传，不做猜测性降级。"""
    assert normalize_effort("medium", GLM) == "medium"


def test_normalized_effort_is_used_on_outbound():
    out = inject_deepseek_reasoning(_body(DS, reasoning_effort="medium"))
    assert out["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# prepare_outbound_body 集成
# ---------------------------------------------------------------------------

def test_prepare_outbound_applies_off_translation():
    out = prepare_outbound_body(_body(DS, reasoning_effort="off"))
    assert out["thinking"] == {"type": "disabled"}
    assert out.get("reasoning_effort") is None


def test_prepare_outbound_default_effort_does_not_mask_model_table():
    """default_effort 缺省时必须走模型档位表，不能被硬编码 high 掩盖。"""
    out = prepare_outbound_body(_body(DS, reasoning_effort="minimal"))
    assert out["reasoning_effort"] == "low"   # 归一结果，而非 high
