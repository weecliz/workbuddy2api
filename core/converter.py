#!/usr/bin/env python3
"""
workbuddy2api — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# 连接池：减少重复 TLS 握手； MaxIdleConnsPerHost=20 设计。
_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


# ---------------------------------------------------------------------------
# 推理（思维链）相关处理
#
# 口径对齐参考实现 workbuddy2api-hub 的 wb_proxy.py（backfill_reasoning_content /
# build_upstream_body 的 thinking 注入段）。以下两条只对 DeepSeek 系生效。
# ---------------------------------------------------------------------------

def _is_deepseek(model) -> bool:
    return bool(model) and str(model).lower().startswith("deepseek")


def backfill_reasoning_content(messages: list, model, thinking_enabled=None) -> list:
    """给 assistant 消息补齐 reasoning_content（仅 DeepSeek）。

    背景（hub 逆向注释）：上游在 thinking 模式下要求**每条** assistant 消息都带
    `reasoning_content` 字符串。客户端常常写不回旧轮的思维链（历史里缺失），
    这些历史会被上游拒。

    两半条件（缺一不可）：
      - thinkingEnabled：只要 thinking 开启就回填，不依赖历史里已有痕迹
      - hasTrace：历史里已经有 reasoning（哪怕 thinking 关闭）也回填

    处理细节：
      - `reasoning_content` 非字符串（null/数字/缺失）一律视为缺失，用 `reasoning`
        兜底，再不行用空串（对齐官方 `typeof !== "string"` 检查）
      - 同时把值镜像到 `reasoning` 且**保证非空**：上游校验非空，
        而单个空格能过长度校验且不携带模型可见语义
    """
    if not _is_deepseek(model):
        return messages
    if thinking_enabled is None:
        thinking_enabled = False

    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        r = m.get("reasoning")
        if isinstance(r, str) and r:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages

    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
                item["reasoning_content"] = rc
            existing = item.get("reasoning")
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out


def resolve_thinking_state(body: dict, model) -> bool:
    """判断本次请求是否处于「思考开启」状态（仅 DeepSeek）。

    退出条件（任一命中即视为不思考）：
      - `thinking.type == "disabled"`
      - `reasoning_effort` / `reasoningEffort` 为 "none"
    默认视为开启 —— 这与官方“不显式关就思考”的行为一致。
    """
    if not _is_deepseek(model):
        return False
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if str(thinking.get("type") or "").strip().lower() == "disabled":
            return False
    effort = body.get("reasoning_effort") or body.get("reasoningEffort")
    if str(effort or "").strip().lower() == "none":
        return False
    return True


def inject_deepseek_reasoning(body: dict, default_effort: str = "high") -> dict:
    """出站前的 DeepSeek 推理处理：档位兜底 + reasoning_content 回填（原地修改并返回）。

    为什么需要档位兜底（hub 实测，deepseek-v4.1-flash、同一 prompt）：
        enabled + 无档位  -> reasoning_tokens 0,   reasoning_content len 0
        reasoning_effort=high -> reasoning_tokens 37, reasoning_content len 117
    即：只开 thinking 而不带档位，上游仍按“不思考”应答，思维链被静默丢弃。

    原则（与 hub 一致）：
      - 客户端显式指定的档位**永不覆盖**
      - `thinking.type=disabled` / `effort=none` 照常退出，不被迫思考
      - 只对 DeepSeek 系生效
    """
    model = body.get("model")
    if not _is_deepseek(model):
        # 非 DeepSeek：不注入任何推理参数，但要把 Anthropic 侧的临时意图键清掉。
        # 否则它会随 body 一起发往上游（无效字段，无价值且可能引起校验问题）。
        if THINKING_INTENT_KEY in body:
            body.pop(THINKING_INTENT_KEY, None)
        return body

    # 先把 Anthropic 遗留的思考意图落实成后端参数（非 DeepSeek 时丢弃）。
    # 必须在模型已知之后做：_map_anthropic_model 会把 claude-sonnet-4 之类
    # 映射到本号池的真实模型，映射前就写 effort 会误带到非 DeepSeek 模型上。
    # 也必须在下面「档位兜底」之前做 —— 否则兜底先写入 high，
    # 客户端在 thinking.effort 里指定的 low 就会被 setdefault 挡掉。
    apply_thinking_intent(body, model)

    thinking = body.get("thinking")
    opted_out = isinstance(thinking, dict) and \
        str(thinking.get("type") or "").strip().lower() == "disabled"
    effort = body.get("reasoning_effort") or body.get("reasoningEffort")
    # 档位也可能写在 thinking.effort 里（Anthropic 风格，也常见于 OpenAI 兼容
    # 客户端）——只看顶层字段会漏判，后续兜底就会把客户端显式要的 low 盖成 high。
    if not effort and isinstance(thinking, dict):
        inner = thinking.get("effort")
        if isinstance(inner, str) and inner.strip():
            effort = inner.strip()

    if not opted_out and str(effort or "").strip().lower() != "none":
        if "thinking" not in body:
            body["thinking"] = {"type": "enabled"}
        # 档位优先级：顶层 reasoning_effort > thinking.effort > 默认 high。
        # 后两者都要写回 body：thinking.effort 只是局部变量，不写回的话
        # 出站时仍只会看到顶层的 high（客户端显式要的 low 就白说了）。
        body["reasoning_effort"] = effort or default_effort

    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        body["messages"] = backfill_reasoning_content(
            messages, model, thinking_enabled=resolve_thinking_state(body, model))
    return body


def _client_ip_headers(request: Request, purpose: str = "conversation") -> dict:
    """提取真实客户端 IP 与用途/产品头，透传给上游，避免请求用量里 client/agentPurpose 为空。

    真实 WorkBuddy 桌面端：
      - X-Agent-Purpose: "conversation" 用于普通对话
      - X-IDE-Name / X-IDE-Type / X-Product: "WorkBuddy" 用于上游识别 client
    """
    ip = None
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        ip = xff.split(",")[0].strip()
    else:
        real = request.headers.get("X-Real-IP")
        if real:
            ip = real.strip()
        elif request.client:
            ip = request.client.host
    client_name = os.environ.get("ADMIN_UPSTREAM_CLIENT_NAME", "WorkBuddy").strip() or "WorkBuddy"
    h = {
        "X-Agent-Purpose": purpose or "conversation",
        "X-IDE-Name": client_name,
        "X-IDE-Type": client_name,
        "X-Product": client_name,
    }
    if ip:
        h["X-Forwarded-For"] = ip
        h["X-Real-IP"] = ip
        h["X-Client-IP"] = ip
        custom = os.environ.get("ADMIN_UPSTREAM_CLIENT_HEADER", "").strip()
        if custom:
            h[custom] = ip
    return h

try:
    from .desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from .fingerprint import device_headers as _device_headers
from .responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
    custom_tool_names,
)
from .responses_projection import project_responses_chat_body
from .anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
    apply_thinking_intent,
    THINKING_INTENT_KEY,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ⚠️ 上面这个常量是历史遗留：改动前所有账号的出站 UA 都是它。
#    现在 UA 由客户端身份画像决定（见下），该常量**已不再用于出站请求**，
#    仅为兼容可能的外部引用而保留。
#    想恢复旧行为可设 ADMIN_UA_WORKBUDDY=codebuddy2openai/2.0。

# ---------------------------------------------------------------------------
# 客户端身份画像（按 auth.domain 现算，无状态、不落库）
#
# 官方两个客户端（WorkBuddy 桌面端 / CodeBuddy CLI）在服务端看来是**同一套客户端**：
# 各自 product.json 里 platform 都是 "CLI"、deploymentType 都是 "SaaS"（即 X-Product）、
# authentication.attributes.prefixPath 都是 "/plugin"，refresh 都用
# X-Auth-Refresh-Source: plugin。端点与请求头结构完全一致。
#
# 唯一会体现在出站请求里的实质差异是 **User-Agent 里的 productName**：
#     WorkBuddy 桌面端 -> "CLI/<ver> WorkBuddy/<ver>"
#     CodeBuddy CLI   -> "CLI/<ver> CodeBuddy/<ver>"
# 次要差异：桌面端会给「模型 / 签到」请求带 X-Device-Token，CLI 官方不带。
#
# 身份来源：凭据自身的 auth.domain（实测 WorkBuddy 桌面端是 www.workbuddy.cn，
# CodeBuddy CLI 是 copilot.tencent.com）。每次出站时由 `_build_headers_from`
# 现算，无需任何外部存储。domain 缺失时回落全局默认
# ADMIN_UPSTREAM_CLIENT_KIND（默认 workbuddy）。
#
# 版本号可信度：
#   - codebuddy 的值是**本机实测**（取自 ~/.codebuddy/logs 里的真实请求头）。
#   - workbuddy 的值按官方 UA 拼装规则（platform/productName 拼装）与其 product.json
#     **推断**得出；桌面端不打印 API 请求头，无法从日志确认版本号，故以应用版本
#     5.3.14 作默认值。客户端升级后请同步调整这两个环境变量。
# ---------------------------------------------------------------------------
CLIENT_KINDS = ("workbuddy", "codebuddy")

_KIND_UA = {
    "workbuddy": os.getenv("ADMIN_UA_WORKBUDDY", "CLI/5.3.14 WorkBuddy/5.3.14"),
    "codebuddy": os.getenv("ADMIN_UA_CODECLI", "CLI/2.148.0 CodeBuddy/2.148.0"),
}


# ---------------------------------------------------------------------------
# 设备指纹头（X-Machine-ID / X-Session-ID / X-Request-ID）
#
# 与 X-Device-Token 的区别（别混）：
#   - X-Device-Token 由桌面端 Turing SDK 生成，**依赖本机安装**，容器 / 云端
#     必然拿不到，取不到时就降级为不带该头；
#   - 下面三个头由 uid 纯哈希派生（core/fingerprint.py），**不依赖任何外部组件**，
#     任何部署环境都算得出来。
#
# 开关默认 on（对齐参考实现 workbuddy2api-hub 的「无条件注入」行为）。
# 之所以不做「跟随 X-Device-Token 是否可得」的联动：两者机制无关，联动只会让
# 云端部署（拿不到 Device-Token）白白失去「同账号设备稳定 + 多账号彼此隔离」
# 这项收益，而这三个头不是官方客户端指纹、不涉及伪造官方特征。
# 需要回滚时设 ADMIN_DEVICE_FINGERPRINT=off。
# ---------------------------------------------------------------------------

def device_fingerprint_enabled() -> bool:
    """是否注入三个设备指纹头（默认开；仅 'off'/'0'/'false'/'no' 关闭）。"""
    raw = (os.getenv("ADMIN_DEVICE_FINGERPRINT") or "on").strip().lower()
    return raw not in ("off", "0", "false", "no")


def client_kind_from_domain(domain: str | None) -> str:
    """按凭据里的 auth.domain 推断客户端身份（无状态，出站时现算）。

    domain 含 "workbuddy" -> workbuddy；其他非空 -> codebuddy；
    空 / 缺失 -> 回落全局默认 ADMIN_UPSTREAM_CLIENT_KIND（默认 workbuddy）。

    ⚠️ 回落默认与历史行为不同：改动前所有账号的 UA 都是自造的
    `codebuddy2openai/2.0`，现在会拿到真实客户端 UA。这是有意的修正 ——
    自造名本身就是画像不自洽的风险点。想完全恢复旧行为，
    设 `ADMIN_UA_WORKBUDDY=codebuddy2openai/2.0`。
    """
    d = (domain or "").strip().lower()
    if "workbuddy" in d:
        return "workbuddy"
    if d:
        return "codebuddy"
    default = (os.getenv("ADMIN_UPSTREAM_CLIENT_KIND") or "workbuddy").strip().lower()
    return default if default in _KIND_UA else "workbuddy"


def client_kind_ua(kind: str | None) -> str:
    """取该客户端身份对应的 User-Agent。"""
    k = (kind or "").strip().lower()
    return _KIND_UA.get(k) or _KIND_UA["workbuddy"]

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if not d.is_dir():
            continue
        # 优先用桌面端实时登录文件（无时间戳后缀），避免被历史备份按字典序抢走
        live = d / "workbuddy-desktop.info"
        if live.is_file():
            return live
        files = sorted(d.glob("*.info"))
        if files:
            return files[0]
    return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

def _get_turing_device_token() -> str | None:
    """延迟取本机设备风控 Token；失败返回 None（不影响主流程）。

    放在模块级做懒加载：converter.py 既可作为 admin 的子模块被挂载，也可独立
    `python converter.py` 运行。admin 包不可用时（极少数情况）直接降级为不带该头。
    """
    try:
        from admin.turing_token import get_device_token
        return get_device_token()
    except Exception:
        return None


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。

    出站身份（UA / X-Device-Token 策略）在 `_build_headers_from` 内按凭据的
    auth.domain 现算，无需外部指定。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        # pi-lens-ignore: unchecked-throwing-call-python
        with open(self.path, "r", encoding="utf-8") as f:
            # pi-lens-ignore: unchecked-throwing-call-python
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        # pi-lens-ignore: unchecked-throwing-call-python
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            # pi-lens-ignore: unchecked-throwing-call-python
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            # pi-lens-ignore: unchecked-throwing-call-python
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回；写盘失败由上层 per-account try/except 兜底，不加死代码
        # pi-lens-ignore: unchecked-throwing-call-python
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # pi-lens-ignore: unchecked-throwing-call-python
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        kind = client_kind_from_domain(auth.get("domain"))
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": client_kind_ua(kind),
        }
        # 风控设备头：官方桌面端只给「模型 / 签到」请求带该头，CodeBuddy CLI 官方不带。
        # 因此仅 workbuddy 身份尝试注入；取不到（桌面端未安装 / SDK 不支持 / 云端 Linux）
        # 时优雅降级为不带该头，不影响主流程。
        if kind == "workbuddy":
            tok = _get_turing_device_token()
            if tok:
                h["X-Device-Token"] = tok
        # 稳定设备指纹三头：纯 uid 哈希派生，不依赖 Turing SDK，任何身份、
        # 任何部署环境都注入（含上一步拿不到 X-Device-Token 的云端场景）。
        # 收益是「同账号设备长期稳定 + 多账号彼此隔离」；开关见 device_fingerprint_enabled()。
        if device_fingerprint_enabled():
            h.update(_device_headers(account.get("uid") or ""))
        return h

    def get_headers(self, extra: dict | None = None) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。

        extra: 调用方（如 proxy.py）可注入的风控/审计头，例如真实客户端 IP、
               用途标识 X-Agent-Purpose 等。这些头会被 merge 到基础鉴权头之后，
               确保上游请求用量能正确显示 client 与 agentPurpose，降低被风控概率。
        """
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            h = self._build_headers_from(s.get("auth") or {}, s.get("account") or {})
            if extra:
                h.update(extra)
            return h

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }

    # -----------------------------------------------------------------------
    # 后端资源查询（模型列表、额度）
    # -----------------------------------------------------------------------

    def _request_backend(self, method: str, path: str, json_body: dict | list | None = None) -> dict:
        """向后端发一个同步请求，返回 {code, msg, requestId, data} 或抛异常。"""
        headers = self.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=json_body or {})
        except Exception as e:
            raise RuntimeError(f"后端请求网络失败 {method} {path}: {e}")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"后端返回非 JSON {method} {path} HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200 or data.get("code") != 0:
            raise RuntimeError(f"后端请求失败 {method} {path}: HTTP {r.status_code} / {data.get('msg', data)}")
        return data

    def _request_backend_soft(self, method: str, path: str, json_body: dict | list | None = None) -> dict:
        """同 `_request_backend`，但后端返回业务 code!=0 时**不抛异常**，原样返回解析后的 dict。

        用于签到领取等场景：领取接口的 1001(已领)/1002(无资格)/1003(活动结束) 等业务码
        属于正常业务结果，需要由调用方根据 code 区分处理，而非当作错误抛掉。
        """
        headers = self.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=json_body or {})
        except Exception as e:
            raise RuntimeError(f"后端请求网络失败 {method} {path}: {e}")
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"后端返回非 JSON {method} {path} HTTP {r.status_code}: {r.text[:200]}")

    def _enterprise_path_key(self) -> str:
        """返回模型列表 endpoint 里的 enterprise 段：personal 或 enterpriseId。"""
        s = self._session()
        acct = s.get("account") or {}
        if acct.get("type") == "personal":
            return "personal"
        eid = acct.get("enterpriseId")
        return eid if eid else "personal"

    @staticmethod
    def _parse_credit_multiplier(credits) -> float | None:
        """把 'x0.05' / 'x0.00 credits' 解析成浮点倍率，解析不出返回 None。"""
        if not credits:
            return None
        m = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(credits))
        # pi-lens-ignore: unchecked-throwing-call-python
        return float(m.group(1)) if m else None

    def fetch_models(self) -> list[dict]:
        """获取后端真实模型列表（含 id/name/credits 等元信息）。

        兼容两种返回结构：
          - 顶层 data.models 为对象数组（每项含 id/name/credits...）；
          - 仅 data.agents[].models 为字符串数组时，回退收集并去重。
        """
        eid = self._enterprise_path_key()
        data = self._request_backend("GET", f"/v2/enterprises/{eid}/models")
        payload = data.get("data", {})
        models = payload.get("models")
        if isinstance(models, list) and models:
            return models
        # 兜底：从 agents 里收集模型名
        collected: list[dict] = []
        for a in payload.get("agents", []) or []:
            for m in a.get("models", []) or []:
                if isinstance(m, dict) and m.get("id"):
                    collected.append(m)
                elif isinstance(m, str) and m:
                    collected.append({"id": m})
        seen = set()
        result: list[dict] = []
        for m in collected:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                result.append(m)
        if not result:
            raise RuntimeError("后端模型列表格式异常：缺少 data.models 且 agents 中无模型")
        return result

    def fetch_balance(self) -> dict:
        """获取当前账号积分汇总，仅返回总量与剩余（可用积分）。

        注意：必须使用 CycleCapacityRemain（当前周期剩余），而不是 CapacityRemain。
        CodeBuddy 个人体验版等一次性资源在账号层级 CapacityRemain 仍显示原始额度，
        但当期已用完后 CycleCapacityRemain 为 0；界面上的「累积剩余」也以周期剩余为准。
        """
        data = self._request_backend("POST", "/v2/billing/meter/get-user-resource", {})
        resp = data.get("data", {}).get("Response", {}).get("Data", {}) or {}
        total = 0
        total_size = 0
        for a in resp.get("Accounts") or []:
            if a.get("CapacityUnit") != "credits":
                continue
            # 当前周期剩余才是真实可用额度
            total += a.get("CycleCapacityRemain") or 0
            total_size += a.get("CapacitySize") or 0
        return {
            "total": total_size,    # 总积分
            "remain": total,        # 可用积分（当前周期剩余额度）
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m3-pay", "hy3-preview-agent", "auto",
]

# 后端资源缓存（TTL，秒）
_RESOURCE_CACHE_TTL = 60.0
_MODELS_CACHE = {"ts": 0.0, "data": None, "error": None}
_BALANCE_CACHE = {"ts": 0.0, "data": None, "error": None}

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
    # thinking 必须透传：它是客户端表达「要不要思考」的开关。
    # 缺失时的后果（实测）：客户端发 thinking:{type:"disabled"}，该字段在
    # /gw 路径被白名单丢掉，后续的 DeepSeek 档位兜底看不到「已显式关闭」，
    # 反而补上 thinking=enabled + reasoning_effort=high —— 客户端要求不思考，
    # 却被强制开启思考。透传后兜底逻辑才能正确识别 disabled 并放行。
    "thinking",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None,
                "desensitize": False, "no_compact": False}  # cred: CredentialManager | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    # 用 hmac.compare_digest 做定长比较，而不是 `token != key` —— 后者在第一个
    # 不同字节就返回，比较耗时随匹配前缀长度变化，构成可测量的时序旁路。
    # 同项目的 admin/security.py 已统一用 compare_digest（密码校验、Key 校验）。
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    if not supplied and x_api_key:
        supplied = x_api_key
    if not hmac.compare_digest(supplied, key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred() -> CredentialManager:
    if CONFIG["cred"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["cred"]


def _cached_models(cred) -> list[dict]:
    """带 TTL 缓存的真实模型列表；失败时抛异常由调用方回退。"""
    global _MODELS_CACHE
    now = time.time()
    if _MODELS_CACHE["data"] is not None and now - _MODELS_CACHE["ts"] < _RESOURCE_CACHE_TTL:
        return _MODELS_CACHE["data"]
    try:
        models = cred.fetch_models()
    except Exception as e:
        _MODELS_CACHE["error"] = str(e)
        raise
    _MODELS_CACHE = {"ts": now, "data": models, "error": None}
    return models


def _cached_balance(cred) -> dict:
    """带 TTL 缓存的真实积分额度；失败时抛异常由调用方回退。"""
    global _BALANCE_CACHE
    now = time.time()
    if _BALANCE_CACHE["data"] is not None and now - _BALANCE_CACHE["ts"] < _RESOURCE_CACHE_TTL:
        return _BALANCE_CACHE["data"]
    try:
        balance = cred.fetch_balance()
    except Exception as e:
        _BALANCE_CACHE["error"] = str(e)
        raise
    _BALANCE_CACHE = {"ts": now, "data": balance, "error": None}
    return balance


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_file": str(find_auth_file() or "(未找到)"), "mode": "direct-proxy (native function calling)"}
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
        try:
            info["balance"] = _cached_balance(cred)
        except Exception as e:
            info["balance_error"] = str(e)
    return info


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = CONFIG["cred"]
    if cred is not None:
        try:
            models = _cached_models(cred)
            data = [{
                "id": m.get("id"),
                "object": "model",
                "created": 1700000000,
                "owned_by": "codebuddy",
                "name": m.get("name") or m.get("id"),
                "credits": m.get("credits"),
                "credit_multiplier": cred._parse_credit_multiplier(m.get("credits")),
                "description": m.get("descriptionZh") or m.get("descriptionEn"),
                "supports_images": m.get("supportsImages"),
                "supports_reasoning": m.get("supportsReasoning"),
                "supports_tool_call": m.get("supportsToolCall"),
                "max_input_tokens": m.get("maxInputTokens"),
                "max_output_tokens": m.get("maxOutputTokens"),
                "vendor": m.get("vendor"),
            } for m in models if m.get("id")]
            return {"object": "list", "data": data, "source": "backend"}
        except Exception as e:
            _log(f"获取真实模型列表失败，回退到 DEFAULT_MODELS: {e}")
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in DEFAULT_MODELS]
    return {"object": "list", "data": data, "source": "fallback"}


@app.get("/v1/balance")
def get_balance(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()
    try:
        return {"object": "balance", "source": "backend", **_cached_balance(cred)}
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"获取额度失败：{e}", "type": "upstream_error"}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # DeepSeek 推理处理（档位兜底 + reasoning_content 回填）。放在脱敏之后：
    # 脱敏会重写消息内容，先脱敏再回填才能保证最终出站的 body 已补齐。
    body = inject_deepseek_reasoning(body)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300, limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(raw, r.status_code))
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        # pi-lens-ignore: unchecked-throwing-call-python
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    try:
        async with httpx.AsyncClient(timeout=None, limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120, limits=_HTTP_LIMITS) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(url: str, headers: dict, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    # 客户端声明的 custom（自由格式）工具名，供响应侧还原 custom_tool_call 事件。
    # 必须从原始 payload 取：chat_body 里这些工具的 type 已被改写成 function。
    chat_custom_names = custom_tool_names(payload.get("tools"))

    chat_body = _chat_body_desensitize(chat_body)
    # DeepSeek 推理处理（同 chat_completions：脱敏之后再回填）
    chat_body = inject_deepseek_reasoning(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid, chat_custom_names),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(url, headers, chat_body, rid, model_name)
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name, custom_names=chat_custom_names)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    return JSONResponse(content=result)


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "",
                            custom_names=None):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name, custom_names=custom_names)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, _ = await _post_backend_with_filter_retry(url, headers, body, rid, model_name)
        if status_code != 200:
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    # DeepSeek 推理处理（同 chat_completions：脱敏之后再回填）
    chat_body = inject_deepseek_reasoning(chat_body)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(url, headers, chat_body, model_name, t0, rid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async with httpx.AsyncClient(timeout=None, limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                    yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                    return
                async for line in r.aiter_lines():
                    events = converter.feed_line(line)
                    if events:
                        yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n")
            sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   GET  /v1/balance            (当前账号可用积分额度)\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
