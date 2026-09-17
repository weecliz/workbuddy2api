"""
responses_adapter.py — OpenAI Responses API ↔ Chat Completions API 适配层。

Codex CLI 使用 Responses API（POST /v1/responses），而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
  请求：Responses input/instructions/tools → Chat messages/tools
  响应：Chat SSE delta → Responses 语义事件流（response.created / output_text.delta / …）

事件类型参考：https://developers.openai.com/api/docs/guides/streaming-responses
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# ID 生成
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "resp_") -> str:
    return prefix + os.urandom(12).hex()

# ---------------------------------------------------------------------------
# 请求转换：Responses → Chat
# ---------------------------------------------------------------------------

def responses_request_to_chat(body: dict) -> dict:
    """将 Responses API 请求体转换为 Chat Completions 请求体。

    关键映射：
      input → messages
      instructions → system message（置顶）
      max_output_tokens → max_tokens
      tools 格式微调（Responses 用 name，Chat 用 function.name）
    """
    messages: list[dict] = []

    # instructions → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # input → messages
    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_convert_input_items(inp))

    # 构造 Chat body
    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model
    if "model" in body:
        chat["model"] = body["model"]

    # tools — Responses 和 Chat 的 function tool 格式略有不同
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "reasoning_effort"):
        if key in body:
            chat[key] = body[key]

    # max_output_tokens → max_tokens
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    return chat


def _convert_input_items(items: list) -> list[dict]:
    """将 Responses API 的 input 数组转换为 Chat messages。

    input 里可能包含：
      - {"role": "user/developer", "content": ...}   → 直接映射
      - {"type": "message", ...}                      → 助手消息
      - {"type": "function_call", ...}                → 需合并到前面的助手消息
      - {"type": "custom_tool_call", ...}             → 同上（自由格式工具，input 需包成 JSON）
      - {"type": "function_call_output", ...}         → tool 角色
      - {"type": "custom_tool_call_output", ...}      → tool 角色（同上）
    """
    messages: list[dict] = []
    # 临时缓存：合并相邻的 assistant message 和 function_call
    pending_assistant_content: str | None = None
    pending_tool_calls: list[dict] = []

    def _flush_assistant():
        nonlocal pending_assistant_content, pending_tool_calls
        if pending_assistant_content is not None or pending_tool_calls:
            msg: dict[str, Any] = {"role": "assistant",
                                   "content": pending_assistant_content or ""}
            if pending_tool_calls:
                msg["tool_calls"] = pending_tool_calls[:]
            messages.append(msg)
            pending_assistant_content = None
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role", "")

        # 简单消息 {"role": "user", "content": "..."}
        if item_type is None and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # typed message（Responses 里常见）
        if item_type == "message" and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # assistant 消息（来自前一轮输出）
        if item_type == "message" and role == "assistant":
            _flush_assistant()
            content_parts = item.get("content", [])
            text = _extract_output_text(content_parts) if isinstance(content_parts, list) else str(content_parts)
            pending_assistant_content = text
            continue

        # 简单 role=assistant（无 type 标记）
        if item_type is None and role == "assistant":
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            pending_assistant_content = content
            continue

        # function_call — 合并到前面的 assistant 消息
        if item_type == "function_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })
            continue

        # custom_tool_call — 与 function_call 同样合并进 assistant 消息。
        # 区别：custom 的载荷是自由文本（input），而 Chat 协议的 arguments 必须是
        # JSON 字符串，所以这里要包一层 {"input": "..."}。
        # 不处理的话，多轮对话里历史 custom 工具调用会静默丢失（上下文缺口）。
        if item_type == "custom_tool_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            raw = item.get("input")
            if not isinstance(raw, str):
                raw = json.dumps(raw if raw is not None else "", ensure_ascii=False)
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps({"input": raw}, ensure_ascii=False),
                },
            })
            continue

        # function_call_output / custom_tool_call_output → tool 消息
        # 两者形状一致（call_id + output），共用同一分支。
        # 必须紧随对应 assistant 消息（上游 11148 要求 tool 消息紧跟 tool_calls）。
        if item_type in ("function_call_output", "custom_tool_call_output"):
            _flush_assistant()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": _extract_content(item.get("output", "")),
            })
            continue

        # 其他未知类型 — 尝试当作普通消息
        if role:
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            messages.append({"role": role, "content": content})

    _flush_assistant()
    return messages


def _extract_content(content) -> str:
    """提取 content（可能是 str / list[{type,text}]）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("input_text", "text"):
                    parts.append(p.get("text", ""))
                elif p.get("type") == "output_text":
                    parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts) or str(content)
    return str(content)


def _extract_output_text(content_parts: list) -> str:
    """从 Responses output content parts 提取纯文本。"""
    texts = []
    for part in content_parts:
        if isinstance(part, dict) and part.get("type") == "output_text":
            texts.append(part.get("text", ""))
    return "".join(texts)


# ---------------------------------------------------------------------------
# custom（自由格式）工具
#
# Codex 用 Responses 的 `type: "custom"` 声明自由格式工具（apply_patch 就是这种）：
# 它没有 parameters，只接受一段自由文本。上游 Chat 协议不认该类型，
# 必须降级为「单个 input 字符串参数」的 function 工具，否则工具会被静默丢弃 ——
# 表现为「客户端声明了工具，但模型从不调用」。
#
# 双向都要处理，漏一半就失效：
#   请求侧：_convert_tools_for_chat → _downgrade_custom_tool
#   响应侧：ResponsesStreamConverter(custom_names=...) → custom_tool_call 事件
# ---------------------------------------------------------------------------

CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


def _is_custom_tool(tool) -> bool:
    """是否为 Responses 的自由格式（custom）工具。"""
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools) -> set:
    """请求里声明为 custom 的工具名集合，供响应侧还原 custom_tool_call。

    单独成函数（而不是改动 responses_request_to_chat 的返回值）是为了不动
    既有函数签名 —— 四个调用点都按原签名使用，不必为一项新特性改它们的解包。
    """
    names: set = set()
    for t in tools or []:
        if _is_custom_tool(t) and t.get("name"):
            names.add(str(t["name"]))
    return names


def _downgrade_custom_tool(tool: dict) -> dict:
    """把 Responses custom 工具改写成 Chat function 工具（嵌套格式）。

    本项目上游要的是 `{"type":"function","function":{...}}` 嵌套形状
    （与 _convert_tools_for_chat 的其它分支一致），不是 Responses 的扁平形状。
    """
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        # grammar 是 custom 工具特有字段，Chat 协议无对应位置，附进描述保留信息
        extra = "\n\nGrammar:\n" + str(fmt["definition"])
    return {
        "type": "function",
        "function": {
            "name": tool.get("name") or "",
            "description": (desc + "\n\n" + CUSTOM_TOOL_HINT + extra).strip(),
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Complete raw payload for this tool, verbatim.",
                    }
                },
                "required": ["input"],
            },
        },
    }


def _unwrap_custom_input(args) -> str:
    """把 {"input": "..."} 的参数包拆回自由文本。

    模型可能把整段载荷直接当字符串返回、也可能包成 JSON 对象、
    甚至不是合法 JSON；三种都要能还原，拿不到就原样返回。
    """
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args


def _convert_tools_for_chat(tools: list) -> list:
    """将 Responses 格式的 tools 转为 Chat 格式。

    Responses function 工具：{"type": "function", "name": "shell", "description": ..., "parameters": ...}
    Chat：                   {"type": "function", "function": {"name": "shell", ...}}

    另需处理 Responses 的 **custom（自由格式）工具**（Codex 的 apply_patch 就是这种）：
    它没有 parameters，只有一个自由文本入参，上游 Chat 协议不认该类型。
    处理方式是降级为「单个 input 字符串参数」的 function 工具，
    并在 description 里明确要求原样输出（见 _downgrade_custom_tool）。
    若不处理，custom 工具会被静默丢弃 —— 客户端看起来“工具声明了但模型从不调用”。
    """
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if _is_custom_tool(t):
            result.append(_downgrade_custom_tool(t))
            continue
        if t.get("type") != "function":
            continue
        # 已经是 Chat 格式（有 "function" key）
        if "function" in t:
            result.append(t)
            continue
        # Responses 扁平格式 → Chat 嵌套格式
        fn: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "parameters" in t:
            fn["parameters"] = t["parameters"]
        if "strict" in t:
            fn["strict"] = t["strict"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# 响应转换：Chat → Responses
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """将 Chat SSE 流实时转换为 Responses API 语义事件流。

    用法：
      converter = ResponsesStreamConverter(model="glm-5.2")
      # 对后端返回的每个 SSE 行调 feed_line()
      # feed_line 返回要发送给客户端的 Responses 事件字符串（可能多行）
      for line in backend_sse:
          events = converter.feed_line(line)
          if events:
              yield events.encode()
      # 流结束后调 finish() 获取收尾事件
      yield converter.finish().encode()
    """

    def __init__(self, model: str = "unknown", custom_names=None):
        self.resp_id = _rand_id("resp_")
        self.msg_id = _rand_id("msg_")
        self.model = model
        self.created_at = int(time.time())
        # 客户端声明为 custom（自由格式）的工具名。命中时下游工具调用要还原成
        # custom_tool_call + custom_tool_call_input.* 事件，而非 function_call。
        # 默认空集 → 行为与未支持该特性前完全一致。
        self.custom_names = {str(n) for n in (custom_names or ()) if n}

        # 状态标记
        self._emitted_created = False
        self._emitted_msg_item = False
        self._emitted_content_part = False

        # 累积内容
        self._content = ""
        self._tool_calls: dict[int, dict] = {}  # index → {id, name, args, fc_id, output_idx, emitted}
        self._finish_reason: str | None = None
        self._usage: dict | None = None

    # ---- 公开接口 ----

    def feed_line(self, line: str) -> str:
        """处理一行 SSE（如 'data: {...}'），返回转换后的 Responses 事件字符串。"""
        line = line.strip()
        if not line or not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        return self._process_chunk(chunk)

    def finish(self) -> str:
        """流结束后，发出收尾事件（done + completed）。"""
        events: list[str] = []

        # 关闭 text content
        if self._emitted_content_part:
            events.append(self._evt("response.output_text.done", {
                "output_index": 0, "content_index": 0, "text": self._content
            }))
            events.append(self._evt("response.content_part.done", {
                "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": self._content, "annotations": []}
            }))

        if self._emitted_msg_item:
            events.append(self._evt("response.output_item.done", {
                "output_index": 0,
                "item": self._msg_item("completed")
            }))

        # 关闭 function calls / custom tool calls
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                oi = tc["output_idx"]
                if tc.get("custom"):
                    events.append(self._evt("response.custom_tool_call_input.done", {
                        "output_index": oi,
                        "item_id": tc["fc_id"],
                        "call_id": tc["id"],
                        "input": _unwrap_custom_input(tc["args"]),
                    }))
                else:
                    events.append(self._evt("response.function_call_arguments.done", {
                        "output_index": oi, "arguments": tc["args"]
                    }))
                events.append(self._evt("response.output_item.done", {
                    "output_index": oi, "item": self._fc_item(tc, "completed")
                }))

        # response.completed
        events.append(self._evt("response.completed", {
            "response": self._response_obj("completed")
        }))
        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """流结束后获取完整的非流式 Response 对象。"""
        return self._response_obj("completed")

    # ---- 内部 ----

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        # 模型名
        if chunk.get("model"):
            self.model = chunk["model"]

        # 首次 → 发 created + in_progress
        if not self._emitted_created:
            resp = self._response_obj("in_progress")
            events.append(self._evt("response.created", {"response": resp}))
            events.append(self._evt("response.in_progress", {"response": resp}))
            self._emitted_created = True

        # usage
        if chunk.get("usage"):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # ---- content delta ----
            content = delta.get("content")
            if content:
                if not self._emitted_msg_item:
                    events.append(self._evt("response.output_item.added", {
                        "output_index": 0,
                        "item": self._msg_item("in_progress", empty=True)
                    }))
                    self._emitted_msg_item = True

                if not self._emitted_content_part:
                    events.append(self._evt("response.content_part.added", {
                        "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}
                    }))
                    self._emitted_content_part = True

                self._content += content
                events.append(self._evt("response.output_text.delta", {
                    "output_index": 0, "content_index": 0, "delta": content
                }))

            # ---- tool_calls delta ----
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_calls:
                    # 计算 output_index：msg 占 0，function_call 从 1 开始（如果有 msg）
                    base = 1 if (self._emitted_msg_item or self._content) else 0
                    oi = base + len(self._tool_calls)
                    nm = (tc.get("function") or {}).get("name") or ""
                    is_custom = bool(nm) and nm in self.custom_names
                    self._tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "fc_id": _rand_id("ctc_" if is_custom else "fc_"),
                        "output_idx": oi,
                        "emitted": False,
                        "custom": is_custom,
                    }
                slot = self._tool_calls[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]
                    # 名字可能晚于首个 chunk 才到（首块常只带 id），届时补判 custom。
                    # 注：此时 output_item.added 可能已按 function_call 发过，
                    # 但收尾的 item.done 与 delta 事件会按 custom 正确发出。
                    if fn["name"] in self.custom_names:
                        slot["custom"] = True

                if not slot["emitted"]:
                    # 确保 msg item 已发出（即使 content 为空）
                    if not self._emitted_msg_item and (self._content or not self._tool_calls):
                        pass  # 不需要额外处理
                    events.append(self._evt("response.output_item.added", {
                        "output_index": slot["output_idx"],
                        "item": self._fc_item(slot, "in_progress")
                    }))
                    slot["emitted"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    if slot.get("custom"):
                        events.append(self._evt("response.custom_tool_call_input.delta", {
                            "output_index": slot["output_idx"],
                            "item_id": slot["fc_id"],
                            "call_id": slot["id"],
                            "delta": fn["arguments"],
                        }))
                    else:
                        events.append(self._evt("response.function_call_arguments.delta", {
                            "output_index": slot["output_idx"],
                            "delta": fn["arguments"]
                        }))

            if finish:
                self._finish_reason = finish

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """格式化一个 SSE 事件。"""
        payload = {"type": event_type, **data}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _msg_item(self, status: str = "in_progress", empty: bool = False) -> dict:
        content = [] if empty else [
            {"type": "output_text", "text": self._content, "annotations": []}
        ]
        return {
            "type": "message",
            "id": self.msg_id,
            "status": status,
            "role": "assistant",
            "content": content,
        }

    def _fc_item(self, tc: dict, status: str) -> dict:
        """构造工具调用 output item；custom 工具返回 custom_tool_call 形状。

        custom 的载荷就是自由文本（不是 JSON 字符串），必须解包后放进 `input`，
        否则 Codex 会拿到 `{"input":"*** Begin Patch..."}` 这种 JSON 包而无法识别。
        """
        if tc.get("custom"):
            return {
                "type": "custom_tool_call",
                "id": tc["fc_id"],
                "call_id": tc["id"],
                "name": tc["name"],
                "input": _unwrap_custom_input(tc["args"]) if status != "in_progress" else "",
                "status": status,
            }
        return {
            "type": "function_call",
            "id": tc["fc_id"],
            "call_id": tc["id"],
            "name": tc["name"],
            "arguments": tc["args"],
            "status": status,
        }

    def _response_obj(self, status: str) -> dict:
        output = []
        if self._emitted_msg_item or self._content:
            output.append(self._msg_item(status))
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                output.append(self._fc_item(tc, status))

        usage = None
        if self._usage:
            u = self._usage
            usage = {
                "input_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)),
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": u.get("completion_tokens", u.get("output_tokens", 0)),
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": u.get("total_tokens", 0),
            }

        return {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "parallel_tool_calls": True,
            "usage": usage,
        }
