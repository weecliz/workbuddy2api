"""custom（自由格式）工具端到端协议转换测试。

覆盖三处必须同时成立的改动，任一处回归都会让 Codex 的 apply_patch 失效：
  1. 请求侧：custom 工具降级为「单个 input 参数」的 function 工具
  2. 投影侧：降级后的工具与其 description 不被剥掉
  3. 响应侧：调用被还原为 custom_tool_call + custom_tool_call_input.* 事件

全程无网络：只喂合成 chunk，断言 Responses 协议形状。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.responses_adapter import (  # noqa: E402
    CUSTOM_TOOL_HINT,
    ResponsesStreamConverter,
    custom_tool_names,
    responses_request_to_chat,
)
from core.responses_projection import project_responses_chat_body  # noqa: E402

CUSTOM_TOOL = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Use the patch format to edit files",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/s"},
}
FUNC_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "weather",
    "parameters": {"type": "object", "properties": {}},
}
PATCH = "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch"


def _collect(converter, chunks):
    """喂入若干 chat chunk，返回完整的事件流文本。"""
    out = ""
    for delta, finish in chunks:
        line = "data: " + json.dumps(
            {"choices": [{"delta": delta, "finish_reason": finish}]}
        )
        out += converter.feed_line(line)
    out += converter.finish()
    return out


def _events(text, event_type):
    """从事件流文本里取出指定类型的事件 payload。"""
    found = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = json.loads(line[6:])
        if payload.get("type") == event_type:
            found.append(payload)
    return found


# ---------------------------------------------------------------------------
# 1. 请求侧：custom 工具降级
# ---------------------------------------------------------------------------

def test_custom_tool_downgraded_to_single_input_function():
    chat = responses_request_to_chat({"model": "m", "input": "hi",
                                      "tools": [CUSTOM_TOOL, FUNC_TOOL]})
    tools = chat["tools"]

    assert len(tools) == 2, "custom 工具被丢弃了"
    fn = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert fn["name"] == "apply_patch"
    assert list(fn["parameters"]["properties"]) == ["input"]
    assert fn["parameters"]["required"] == ["input"]
    assert CUSTOM_TOOL_HINT in fn["description"]
    assert "start: /.*/s" in fn["description"], "grammar 未保留"


def test_ordinary_function_tool_untouched():
    chat = responses_request_to_chat({"model": "m", "input": "hi",
                                      "tools": [FUNC_TOOL]})
    fn = chat["tools"][0]["function"]
    assert fn["name"] == "get_weather"
    assert fn["parameters"] == {"type": "object", "properties": {}}
    assert "freeform" not in fn.get("description", "")


def test_custom_tool_names_extracted():
    assert custom_tool_names([CUSTOM_TOOL, FUNC_TOOL]) == {"apply_patch"}
    assert custom_tool_names([]) == set()
    assert custom_tool_names(None) == set()


def test_custom_history_roundtrip():
    """前一轮的 custom_tool_call / output 必须能转成 chat 的 tool_calls / tool。"""
    hist = {"model": "m", "input": [
        {"role": "user", "content": "edit the file"},
        {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_1",
         "input": PATCH},
        {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done!"},
    ]}
    msgs = responses_request_to_chat(hist)["messages"]
    asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(asst) == 1
    tc = asst[0]["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert json.loads(tc["function"]["arguments"])["input"].startswith("*** Begin Patch")
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_1"


# ---------------------------------------------------------------------------
# 2. 投影侧：降级后的工具与描述不被剥掉
# ---------------------------------------------------------------------------

def test_projection_keeps_custom_tool_and_description():
    chat = responses_request_to_chat({"model": "m", "input": "hi",
                                      "tools": [CUSTOM_TOOL, FUNC_TOOL]})
    projected, stats = project_responses_chat_body(chat)

    assert stats["original_tools"] == 2
    assert stats["projected_tools"] == 2, "投影阶段丢弃了工具"
    names = [t["function"]["name"] for t in projected["tools"]]
    assert names == ["apply_patch", "get_weather"]
    # 描述是模型判断「何时调用、怎么填参」的主要依据 —— 剥掉等于降级失效
    assert CUSTOM_TOOL_HINT in projected["tools"][0]["function"]["description"]
    assert projected["tools"][1]["function"]["description"] == "weather"


def test_projection_keeps_property_level_description():
    tool = {"type": "function", "name": "f", "description": "d", "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "CITY NAME"}},
    }}
    projected, _ = project_responses_chat_body({"messages": [], "tools": [tool]})
    props = projected["tools"][0]["function"]["parameters"]["properties"]
    assert props["city"].get("description") == "CITY NAME"


# ---------------------------------------------------------------------------
# 3. 响应侧：custom_tool_call 事件
# ---------------------------------------------------------------------------

def _custom_stream(custom_names):
    """模拟上游把 custom 工具调用分块吐出（arguments 是 JSON 包）。"""
    cid = f'{{"input":{json.dumps(PATCH)}}}'
    head, tail = cid[:20], cid[20:]
    return _collect(ResponsesStreamConverter(model="m", custom_names=custom_names), [
        ({"tool_calls": [{"index": 0, "id": "call_9",
                          "function": {"name": "apply_patch", "arguments": ""}}]}, None),
        ({"tool_calls": [{"index": 0, "function": {"arguments": head}}]}, None),
        ({"tool_calls": [{"index": 0, "function": {"arguments": tail}}]}, None),
        ({}, "tool_calls"),
    ])


def test_stream_custom_tool_emits_custom_events():
    text = _custom_stream({"apply_patch"})

    assert "response.custom_tool_call_input.delta" in text
    assert "response.custom_tool_call_input.done" in text
    assert "response.function_call_arguments" not in text, "custom 工具不该发 function 事件"

    done = _events(text, "response.custom_tool_call_input.done")
    assert done, "缺少 done 事件"
    # 载荷必须是自由文本原文，不能是 {"input": "..."} 的 JSON 包
    assert done[0]["input"] == PATCH, repr(done[0]["input"])

    added = _events(text, "response.output_item.added")
    assert [a["item"]["type"] for a in added] == ["custom_tool_call"]
    completed = _events(text, "response.output_item.done")
    assert completed[0]["item"]["type"] == "custom_tool_call"
    assert completed[0]["item"]["input"] == PATCH


def test_stream_ordinary_tool_unaffected():
    """回归：普通工具即使在 custom_names 命中其他名字时也走原路径。"""
    text = _collect(ResponsesStreamConverter(model="m", custom_names={"apply_patch"}), [
        ({"tool_calls": [{"index": 0, "id": "call_10",
                          "function": {"name": "get_weather", "arguments": ""}}]}, None),
        ({"tool_calls": [{"index": 0,
                          "function": {"arguments": '{"city":"Beijing"}'}}]}, None),
        ({}, "tool_calls"),
    ])
    assert "response.function_call_arguments.delta" in text
    assert "response.function_call_arguments.done" in text
    assert "custom_tool_call_input" not in text
    done = _events(text, "response.function_call_arguments.done")
    assert done[0]["arguments"] == '{"city":"Beijing"}'


def test_stream_without_custom_names_keeps_legacy_behaviour():
    """回归：请求未声明 custom 工具时，行为与引入该特性前完全一致。"""
    text = _custom_stream(None)
    assert "response.function_call_arguments.done" in text
    assert "custom_tool_call_input" not in text


def test_nonstream_custom_tool_reinflated():
    """非流式路径：custom 调用同样要还原成 custom_tool_call。"""
    converter = ResponsesStreamConverter(model="m", custom_names={"apply_patch"})
    body = f'{{"input":{json.dumps(PATCH)}}}'
    text = _collect(converter, [
        ({"tool_calls": [{"index": 0, "id": "call_7",
                          "function": {"name": "apply_patch", "arguments": body}}]}, "tool_calls"),
    ])
    assert "custom_tool_call_input" in text
    obj = converter.get_nonstream_response()
    items = obj["output"]
    assert items[0]["type"] == "custom_tool_call"
    assert items[0]["input"] == PATCH
    assert items[0]["call_id"] == "call_7"


# ---------------------------------------------------------------------------
# 4. _unwrap_custom_input 的容错
# ---------------------------------------------------------------------------

def test_unwrap_handles_non_json_and_wrapped_shapes():
    from core.responses_adapter import _unwrap_custom_input

    assert _unwrap_custom_input(json.dumps({"input": PATCH})) == PATCH
    assert _unwrap_custom_input(json.dumps("plain")) == "plain"
    assert _unwrap_custom_input("not json at all") == "not json at all"
    # dict 里 input 不是字符串时就地序列化，不丢内容
    assert json.loads(_unwrap_custom_input(json.dumps({"input": {"a": 1}}))) == {"a": 1}
