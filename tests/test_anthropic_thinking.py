"""Anthropic 端点的思考透传（响应侧）。

背景：请求侧早已把 `thinking` 翻译成 `reasoning_effort`（见
tests/test_thinking_translation.py），但 `AnthropicStreamConverter` 只认
`delta.content` 与 `delta.tool_calls`，**从不读 `delta.reasoning_content`** ——
上游即使产出了思维链，Claude Code / CC Switch 也收不到任何 thinking 块。
本文件锁定修复后的行为。

覆盖点：
  - thinking 块必须排在 text / tool_use 之前，且关闭后再开下一个块
  - 流式要发 `thinking_delta`，并在 `content_block_stop` 前补 `signature_delta`
  - 非流式聚合的 content 数组里也要有 thinking 块（含 signature 字段）
  - 乱序（正文先出、reasoning 后到）时丢弃迟到的思考，不破坏块顺序
  - 没有 reasoning 时行为与此前一致（不凭空冒出 thinking 块）
"""
from __future__ import annotations

import json
from typing import Any

from core.anthropic_adapter import (
    THINKING_INTENT_KEY,
    AnthropicStreamConverter,
    anthropic_request_to_chat,
)


def _sse(**delta: Any) -> str:
    """构造一行上游 Chat SSE（delta 为空时用 finish_reason 收尾）。"""
    chunk = {"model": "deepseek-v4.1-flash", "choices": [{"index": 0, "delta": delta}]}
    return "data: " + json.dumps(chunk, ensure_ascii=False)


def _sse_finish(reason: str = "stop") -> str:
    chunk = {"model": "deepseek-v4.1-flash",
             "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    return "data: " + json.dumps(chunk, ensure_ascii=False)


def _events(blob: str) -> list[str]:
    """从 Anthropic SSE 文本里抽出事件类型序列。"""
    return [ln[len("event: "):] for ln in blob.splitlines() if ln.startswith("event: ")]


def _data(blob: str) -> list[dict]:
    """从 Anthropic SSE 文本里抽出所有事件的 JSON 体。"""
    return [json.loads(ln[len("data: "):]) for ln in blob.splitlines()
            if ln.startswith("data: ")]


def _run(lines: list[str]) -> tuple[str, AnthropicStreamConverter]:
    conv = AnthropicStreamConverter(model="deepseek-v4.1-flash")
    blob = "".join(conv.feed_line(ln) for ln in lines) + conv.finish()
    return blob, conv


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------

def test_reasoning_becomes_leading_thinking_block():
    blob, _ = _run([
        _sse(reasoning_content="让我想想…"),
        _sse(reasoning_content="因为 1+1=2。"),
        _sse(content="答案是 2。"),
        _sse_finish(),
    ])
    assert _events(blob) == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_delta",   # signature_delta
        "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]

    datas = _data(blob)
    starts = [d for d in datas if d["type"] == "content_block_start"]
    assert starts[0]["index"] == 0
    assert starts[0]["content_block"] == {"type": "thinking", "thinking": ""}
    assert starts[1]["index"] == 1
    assert starts[1]["content_block"]["type"] == "text"

    think_deltas = [d for d in datas if d.get("delta", {}).get("type") == "thinking_delta"]
    assert [d["delta"]["thinking"] for d in think_deltas] == ["让我想想…", "因为 1+1=2。"]
    assert all(d["index"] == 0 for d in think_deltas)

    text_deltas = [d for d in datas if d.get("delta", {}).get("type") == "text_delta"]
    assert [d["delta"]["text"] for d in text_deltas] == ["答案是 2。"]
    assert all(d["index"] == 1 for d in text_deltas)


def test_signature_delta_precedes_thinking_block_stop():
    blob, _ = _run([_sse(reasoning_content="想"), _sse_finish()])
    datas = _data(blob)
    sig_idx = next(i for i, d in enumerate(datas)
                   if d.get("delta", {}).get("type") == "signature_delta")
    stop_idx = next(i for i, d in enumerate(datas)
                    if d["type"] == "content_block_stop" and d["index"] == 0)
    assert sig_idx < stop_idx, "signature_delta 必须排在 content_block_stop 之前"
    assert datas[sig_idx]["delta"]["signature"], "signature 不能为空"


def test_without_reasoning_no_thinking_block():
    """没有 reasoning 时，行为与改造前完全一致。"""
    blob, conv = _run([_sse(content="你好"), _sse_finish()])
    assert "thinking" not in blob
    assert _events(blob) == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]
    assert conv.build_message()["content"] == [{"type": "text", "text": "你好"}]


def test_late_reasoning_is_dropped_not_reordered():
    """正文已经开始后到达的 reasoning 只能丢弃 —— 块顺序不可回退。"""
    blob, conv = _run([
        _sse(content="正文"),
        _sse(reasoning_content="迟到的思考"),
        _sse_finish(),
    ])
    assert "thinking_delta" not in blob
    assert conv.build_message()["content"] == [{"type": "text", "text": "正文"}]


def test_thinking_block_closes_before_tool_use():
    blob, _ = _run([
        _sse(reasoning_content="要调工具"),
        _sse(tool_calls=[{"index": 0, "id": "call_1",
                          "function": {"name": "f", "arguments": "{}"}}]),
        _sse_finish("tool_calls"),
    ])
    datas = _data(blob)
    think_stop = next(i for i, d in enumerate(datas)
                      if d["type"] == "content_block_stop" and d["index"] == 0)
    tool_start = next(i for i, d in enumerate(datas)
                      if d["type"] == "content_block_start"
                      and d["content_block"]["type"] == "tool_use")
    assert think_stop < tool_start
    assert datas[tool_start]["index"] == 1


def test_empty_reasoning_field_is_ignored():
    blob, _ = _run([_sse(reasoning_content=""), _sse(reasoning_content=None),
                    _sse(content="hi"), _sse_finish()])
    assert "thinking" not in blob


# ---------------------------------------------------------------------------
# 非流式聚合
# ---------------------------------------------------------------------------

def test_nonstream_content_puts_thinking_first():
    _, conv = _run([
        _sse(reasoning_content="推理"),
        _sse(content="结论"),
        _sse_finish(),
    ])
    content = conv.build_message()["content"]
    assert [b["type"] for b in content] == ["thinking", "text"]
    assert content[0]["thinking"] == "推理"
    assert content[0]["signature"], "thinking 块必须带非空 signature"
    assert content[1]["text"] == "结论"


def test_nonstream_thinking_only_response():
    _, conv = _run([_sse(reasoning_content="纯思考"), _sse_finish()])
    content = conv.build_message()["content"]
    # 无正文时不补空的 text 块，只留 thinking
    assert [b["type"] for b in content] == ["thinking"]
    assert content[0]["thinking"] == "纯思考"


def test_get_nonstream_response_matches_build_message():
    """两条非流式出口（/v1/messages 用 build_message、/gw 用 get_nonstream_response）一致。"""
    lines = [_sse(reasoning_content="推理"), _sse(content="结论"), _sse_finish()]
    _, a = _run(lines)
    _, b = _run(lines)
    assert a.build_message()["content"] == b.get_nonstream_response()["content"]


# ---------------------------------------------------------------------------
# 请求方向（回归：本次改动不碰请求侧）
# ---------------------------------------------------------------------------

def test_request_side_still_only_records_intent():
    chat = anthropic_request_to_chat({
        "model": "claude-sonnet-4-5",
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert chat[THINKING_INTENT_KEY] == "high"
    # 模型尚未映射，绝不能在这里写 DeepSeek 专属参数
    assert "reasoning_effort" not in chat
    assert "thinking" not in chat


def _roundtrip_history(assistant_blocks: list[dict]) -> list[dict]:
    return anthropic_request_to_chat({
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "1+1=?"},
            {"role": "assistant", "content": assistant_blocks},
            {"role": "user", "content": "继续"},
        ],
    })["messages"]


def test_replayed_thinking_block_is_ignored_but_text_survives():
    """客户端把上轮的 thinking 块原样回传时，只取 text，不把它转成上游参数。"""
    msgs = _roundtrip_history([
        {"type": "thinking", "thinking": "想一下", "signature": "d29ya2J1ZGR5MmFwaQ=="},
        {"type": "text", "text": "2"},
    ])
    assert msgs == [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "继续"},
    ]


def test_assistant_message_with_only_thinking_is_dropped():
    """只有 thinking 块的 assistant 消息若原样转换会得到 content=null 的非法消息。

    该形态在本次修复后才可能出现（此前客户端收不到 thinking 块，历史里自然没有），
    所以必须在这里拦住，而不是让上游去报错。
    """
    msgs = _roundtrip_history([
        {"type": "thinking", "thinking": "被截断的思考", "signature": "x"},
    ])
    assert msgs == [
        {"role": "user", "content": "1+1=?"},
        {"role": "user", "content": "继续"},
    ]
