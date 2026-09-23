"""
anthropic_adapter.py — Anthropic Messages API ↔ OpenAI Chat Completions API 适配层。

Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages），
而 CodeBuddy 后端只支持 OpenAI Chat Completions 协议。本模块做双向转换：
  请求：Anthropic Messages 格式 → OpenAI Chat 格式
  响应：OpenAI Chat SSE → Anthropic Messages SSE 事件流

Anthropic Messages API 参考：https://docs.anthropic.com/en/docs/messages
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# ID 生成
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "") -> str:
    return prefix + os.urandom(12).hex()


# ---------------------------------------------------------------------------
# 请求转换：Anthropic → Chat
# ---------------------------------------------------------------------------

def anthropic_request_to_chat(body: dict) -> dict:
    """将 Anthropic Messages API 请求体转换为 OpenAI Chat Completions 请求体。

    关键映射：
      system → messages[0] role=system
      messages[].content (blocks) → content (string) / tool_calls / tool role
      tools[].input_schema → tools[].function.parameters
      thinking → reasoning_effort（见下），metadata 丢弃
    """
    messages: list[dict] = []

    # system → 首条 system 消息
    system = body.get("system")
    if system:
        sys_content = _extract_system_text(system)
        if sys_content:
            messages.append({"role": "system", "content": sys_content})

    # messages → 消息转换
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        messages.extend(_convert_anthropic_message(m))

    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model（透传，不做映射）
    if "model" in body:
        chat["model"] = body["model"]

    # max_tokens
    if "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    # tools
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_anthropic_tools(tools)

    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            chat["tool_choice"] = {"type": tc.get("type", "any"), "function": {"name": tc.get("name", "")}}
        elif isinstance(tc, str):
            chat["tool_choice"] = tc if tc in ("none", "auto", "required") else {"type": "function", "function": {"name": tc}}

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "top_k"):
        if key in body:
            chat[key] = body[key]

    # thinking → 思考意图
    #
    # Anthropic 用 thinking:{type:"enabled", budget_tokens:N} 表达「要思考」，
    # 而后端只认扁平的 reasoning_effort，且**不识别** budget_tokens（实测：传
    # thinking / budget_tokens 都被静默忽略，reasoning_content 为空）。
    #
    # ⚠️ 这里**只记录意图**，不直接写 reasoning_effort。原因 /v1/messages 的
    # 时序是「先调本函数，再用 _map_anthropic_model 把 claude-sonnet-4 映射成
    # 上游真实模型」——在这里就写 effort 的话，即使最终映射到 glm-5.2 这类
    # 非 DeepSeek 模型也会带着 DeepSeek 专属的思考参数出站。
    #
    # 因此：意图放进 __thinking_intent 这个临时键，由调用方在模型确定后
    # 交给 inject_deepseek_reasoning 判定（它只对 deepseek 生效），
    # 并在出站前由 _strip_thinking_intent 清掉这个内部键。
    #
    # 映射口径（与直接上游 xiaofan6ya/workbuddy2api 的 anthropic_adapter 一致）：
    #   type == "disabled" → 不思考
    #   type == "enabled"  → 思考；有 effort 用 effort，否则兜底 high
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        ttype = str(thinking.get("type") or "").strip().lower()
        if ttype == "disabled":
            chat["__thinking_intent"] = "disabled"
        elif ttype == "enabled":
            effort = thinking.get("effort")
            if not (isinstance(effort, str) and effort.strip()):
                effort = "high"
            chat["__thinking_intent"] = effort.strip()
    elif isinstance(thinking, str) and thinking.strip().lower() == "enabled":
        chat["__thinking_intent"] = "high"

    return chat


# 内部临时键：承载 Anthropic thinking 的原始意图，出站前必须清除。
THINKING_INTENT_KEY = "__thinking_intent"


def apply_thinking_intent(chat: dict, model) -> dict:
    """把 __thinking_intent 落实成后端参数，并清除该内部键（原地修改并返回）。

    必须在模型已经映射成上游真实模型**之后**调用：只有到那时才能判断
    本次请求是否真的落在 DeepSeek 系上。
    """
    intent = chat.pop(THINKING_INTENT_KEY, None)
    if not intent:
        return chat
    if not _is_deepseek_model(model):
        return chat          # 非 DeepSeek：丢弃意图，不泄漏 DeepSeek 参数
    if intent == "disabled":
        chat["thinking"] = {"type": "disabled"}
    else:
        chat.setdefault("reasoning_effort", intent)
    return chat


def _is_deepseek_model(model) -> bool:
    return bool(model) and str(model).lower().startswith("deepseek")


def _extract_system_text(system) -> str:
    """提取 system 字段为纯文本字符串。支持 string 和 [{type:text, text:...}] 数组。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """将单个 Anthropic 消息转换为 OpenAI 格式的消息（可能为多条）。"""
    role = msg.get("role", "")
    content = msg.get("content")

    # 简单字符串 content
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    # 空 content
    if not isinstance(content, list) or not content:
        return []

    # content blocks → 需要解析
    blocks = content

    # 检查是否包含 tool_result（role=user 时）
    if role == "user":
        result: list[dict] = []
        text_parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "tool_result":
                # tool_result → 独立的 tool 消息
                tc_id = block.get("tool_use_id", "")
                output = block.get("content", "")
                if isinstance(output, list):
                    output = "".join(
                        b.get("text", "") for b in output if isinstance(b, dict) and b.get("type") == "text"
                    )
                result.append({"role": "tool", "tool_call_id": tc_id, "content": output})
        # 注意顺序：tool_result 必须紧跟在带 tool_calls 的 assistant 之后（OpenAI 协议要求），
        # 因此同一条 user 消息里夹带的文本（如 Claude Code 的 <system-reminder>）只能放到
        # tool 消息【之后】，否则会变成 assistant(tool_calls) → user → tool，触发后端
        # "tool calls and tool results do not match"（code 11148）。
        joined_text = "".join(text_parts)
        if joined_text:
            result.append({"role": "user", "content": joined_text})
        return result

    # assistant 角色
    if role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "tool_use":
                tc = {
                    "id": block.get("id", _rand_id("call_")),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
                tool_calls.append(tc)
        # 只有 thinking 块（无正文、无工具调用）的 assistant 消息，转换后就是
        # {"role":"assistant","content":null} —— 不带 tool_calls 的裸空 assistant
        # 不是合法的 Chat 消息，直接丢弃整条而不是发给上游。
        # 该形态来自客户端回传我们新发出的 thinking 块（模型在思考中途被 max_tokens
        # 截断时，assistant 消息可能只有 thinking）。
        if not text_parts and not tool_calls:
            return []

        msg_out: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg_out["content"] = "".join(text_parts)
        else:
            msg_out["content"] = None
        if tool_calls:
            msg_out["tool_calls"] = tool_calls
        return [msg_out]

    # 其他角色：尝试提取文本
    text = _extract_blocks_text(blocks)
    return [{"role": role, "content": text}] if text else []


def _extract_blocks_text(blocks: list) -> str:
    """从 content blocks 中提取所有 text 块合并为字符串。"""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _convert_anthropic_tools(tools: list) -> list:
    """将 Anthropic 格式的 tools 转为 OpenAI Chat 格式。

    Anthropic:  {"name": "...", "description": "...", "input_schema": {...}}
    Chat:       {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # 已经是 Chat 格式
        if "function" in t:
            result.append(t)
            continue
        fn: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "input_schema" in t:
            fn["parameters"] = t["input_schema"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Anthropic Messages SSE
# ---------------------------------------------------------------------------

# Anthropic 的 thinking 块必须带非空 `signature`（官方 API 用它校验「块由 Claude
# 生成」，并对回传的块做签名比对）。本项目上游是 OpenAI 协议、没有签名机制，而客户端
# 把块回传时又会被 anthropic_request_to_chat 忽略（它只处理 text / tool_use），
# 所以这里填固定占位值即可 —— 只要非空，客户端就存得下、回传得动。
# 取 base64("workbuddy2api")，形态上是合法的 base64 串。
_THINKING_SIGNATURE = "d29ya2J1ZGR5MmFwaQ=="


class AnthropicStreamConverter:
    """将 OpenAI Chat SSE 流实时转换为 Anthropic Messages SSE 事件流。

    用法：
      converter = AnthropicStreamConverter(model="deepseek-v4-pro")
      for line in backend_sse:
          events = converter.feed_line(line)
          if events:
              yield events.encode()
      yield converter.finish().encode()
    """

    def __init__(self, model: str = "unknown"):
        self.msg_id = _rand_id("msg_")
        self.model = model
        self.created_at = int(time.time())

        # 状态
        self._emitted_start = False

        # thinking 内容块（Anthropic 扩展思考，来源是上游的 reasoning_content）
        self._thinking_content = ""
        self._thinking_block_open = False
        self._thinking_block_idx = 0
        # 正文 / 工具调用是否已经开始。Anthropic 的块顺序是 thinking → text → tool_use，
        # 该标记一旦置位，之后到达的 reasoning 就只能丢弃（流式无法插回前面）。
        self._saw_other_block = False

        # text 内容块
        self._text_content = ""
        self._text_block_open = False
        self._text_block_idx = 0

        # tool_use 内容块（index → {id, name, args, block_idx, open}）
        self._tool_uses: dict[int, dict] = {}
        self._next_block_idx = 0

        # 结束信息
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._content_filter: bool = False

    # ---- 公开接口 ----

    def feed_line(self, line: str) -> str:
        """处理一行 SSE（如 'data: {...}'），返回 Anthropic SSE 事件字符串。"""
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
        """流结束，发出收尾事件。"""
        events: list[str] = []

        # 关闭 thinking 块（顺序上必须排在其它的块之前）
        self._close_thinking_block(events)

        # 关闭 text 块
        if self._text_block_open:
            events.append(self._evt(
                "content_block_stop", {"index": self._text_block_idx}
            ))
            self._text_block_open = False

        # 关闭 tool_use 块
        for tc in self._tool_uses.values():
            if tc.get("open"):
                events.append(self._evt(
                    "content_block_stop", {"index": tc["block_idx"]}
                ))
                tc["open"] = False

        # stop_reason 映射
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        # message_delta
        delta: dict[str, str | None] = {"stop_reason": stop_reason, "stop_sequence": None}
        usage = None
        if self._usage:
            u = self._usage
            usage = {
                "input_tokens": u.get("prompt_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0),
            }
        events.append(self._evt("message_delta", {"delta": delta, "usage": usage}))

        # message_stop
        events.append(self._evt("message_stop", {}))

        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """获取完整的非流式 Message 响应对象。"""
        content = self._build_content_blocks()
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        resp: dict[str, Any] = {
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": self.model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }
        if self._usage:
            resp["usage"] = {
                "input_tokens": self._usage.get("prompt_tokens", 0),
                "output_tokens": self._usage.get("completion_tokens", 0),
            }
        return resp

    # ---- 内部 ----

    def build_message(self) -> dict:
        """把已消费完的流转成一个完整的 Anthropic Message 对象。

        供非流式（stream=false）响应使用：调用方照常把上游 SSE 逐行喂给
        feed_line()，最后再调本方法拿聚合结果。
        """
        usage = self._usage or {}
        # Chat 的 finish_reason → Anthropic 的 stop_reason
        stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
        return {
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": self._build_content_blocks(),
            "stop_reason": stop_map.get(self._finish_reason or "stop", "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    def error_event(self, message: str, err_type: str = "api_error") -> str:
        """构造 Anthropic 风格的 error 事件（上游全部不可用时返回给客户端）。"""
        return self._evt("error", {"error": {"type": err_type, "message": message}})

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        if chunk.get("model"):
            self.model = chunk["model"]

        # 首次 → message_start
        if not self._emitted_start:
            events.append(self._evt("message_start", {
                "message": {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            }))
            self._emitted_start = True

        if chunk.get("usage"):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # reasoning_content delta（DeepSeek 系思维链）→ Anthropic thinking 块
            reasoning = delta.get("reasoning_content")
            if reasoning:
                self._append_thinking(reasoning, events)

            # content delta
            content = delta.get("content")
            if content:
                self._saw_other_block = True
                self._close_thinking_block(events)
                self._text_content += content
                if not self._text_block_open:
                    self._text_block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    events.append(self._evt("content_block_start", {
                        "index": self._text_block_idx,
                        "content_block": {"type": "text", "text": ""},
                    }))
                    self._text_block_open = True
                events.append(self._evt("content_block_delta", {
                    "index": self._text_block_idx,
                    "delta": {"type": "text_delta", "text": content},
                }))

            # tool_calls delta
            if delta.get("tool_calls"):
                self._saw_other_block = True
                self._close_thinking_block(events)
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_uses:
                    block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    self._tool_uses[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "block_idx": block_idx,
                        "open": False,
                    }
                slot = self._tool_uses[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]

                if not slot["open"]:
                    events.append(self._evt("content_block_start", {
                        "index": slot["block_idx"],
                        "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}},
                    }))
                    slot["open"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    events.append(self._evt("content_block_delta", {
                        "index": slot["block_idx"],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    }))

            if finish:
                self._finish_reason = finish

                # finish_reason 出现时关闭当前打开的块
                self._close_thinking_block(events)
                if self._text_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._text_block_idx
                    }))
                    self._text_block_open = False

                for tc in self._tool_uses.values():
                    if tc.get("open"):
                        events.append(self._evt("content_block_stop", {
                            "index": tc["block_idx"]
                        }))
                        tc["open"] = False

        return "".join(events)

    def _append_thinking(self, text: str, events: list[str]) -> None:
        """把上游的 reasoning_content 追加到 thinking 块（必要时先开块）。

        只在正文 / 工具调用尚未开始时空转：Anthropic 要求 thinking 块排在所有
        其它的块之前，而流式下无法把内容插回已有块的前面。DeepSeek 系的上游
        本身就是「先 reasoning 后 content」，正常时序不会走到丢弃分支。
        """
        if self._saw_other_block:
            return
        if not self._thinking_block_open:
            self._thinking_block_idx = self._next_block_idx
            self._next_block_idx += 1
            events.append(self._evt("content_block_start", {
                "index": self._thinking_block_idx,
                "content_block": {"type": "thinking", "thinking": ""},
            }))
            self._thinking_block_open = True
        self._thinking_content += text
        events.append(self._evt("content_block_delta", {
            "index": self._thinking_block_idx,
            "delta": {"type": "thinking_delta", "thinking": text},
        }))

    def _close_thinking_block(self, events: list[str]) -> None:
        """关闭已打开的 thinking 块（幂等）。

        关闭前补一个 signature_delta：官方流里它排在 content_block_stop 之前，
        少了它客户端聚合出的块就没有 signature 字段。
        """
        if not self._thinking_block_open:
            return
        events.append(self._evt("content_block_delta", {
            "index": self._thinking_block_idx,
            "delta": {"type": "signature_delta", "signature": _THINKING_SIGNATURE},
        }))
        events.append(self._evt("content_block_stop", {"index": self._thinking_block_idx}))
        self._thinking_block_open = False

    def _evt(self, event_type: str, data: dict) -> str:
        """格式化一个 Anthropic SSE 事件（含 event: 行）。"""
        payload = {"type": event_type, **data}
        return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _build_content_blocks(self) -> list[dict]:
        """构造完整的 content blocks 数组（用于非流式响应）。"""
        blocks: list[dict] = []

        # thinking block（必须排在 text / tool_use 之前）
        if self._thinking_content:
            blocks.append({
                "type": "thinking",
                "thinking": self._thinking_content,
                "signature": _THINKING_SIGNATURE,
            })

        # text block
        if self._text_content or self._text_block_open:
            blocks.append({"type": "text", "text": self._text_content})

        # tool_use blocks
        for _, tc in sorted(self._tool_uses.items()):
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": {},
            }
            # 尝试将 args 解析为 JSON object
            try:
                block["input"] = json.loads(tc["args"])
            except (json.JSONDecodeError, ValueError):
                block["input"] = tc["args"]
            blocks.append(block)

        return blocks
