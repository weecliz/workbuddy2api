"""_proxy_loop 重试骨架的行为测试（proxy.py 重构回归）。

骨架承担五个上游端点共用的「候选模型 × 账号」双循环，协议差异由回调注入。
这里直接驱动 _proxy_loop，monkeypatch 掉选号 / 会话 / HTTP / 记账四类依赖，
用假响应覆盖以下行为：

  1. 成功路径：chunk 转发、err_count 清零（三振联动）、last_used_at、用量记账
  2. 可重试错误（429/5xx/session 死亡）：策略生效 + 换号继续
  3. 不可重试 4xx：emit_client_error 输出后立即返回，不换号
  4. delivered 后异常：已向客户端吐过内容只能中止，不换号
  5. 候选耗尽：emit_exhausted(实际 err_kind)
  6. 无账号：emit_exhausted("no_account", False)
  7. 非流式：_ProxyOutcome 容器（成功 / 400 / 换号），骨架不对外 yield
"""
import asyncio
import json

import pytest

import admin.routers.proxy as proxy


# ---------------------------------------------------------------------------
# 假依赖
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class FakeAcc:
    def __init__(self, id, uid="uid-0001"):
        self.id = id
        self.uid = uid
        self.err_count = 0
        self.status = "active"
        self.cool_until = None
        self.cool_kind = ""
        self.last_err_at = None
        self.last_err_msg = ""
        self.last_used_at = None
        self.balance_remain = 100


class FakeSess:
    def __init__(self, acc):
        self.acc = acc
        self.closed = False

    def get_headers(self, extra=None):
        assert isinstance(extra, dict), "骨架必须透传风控 headers"
        return {"Authorization": "Bearer x", **extra}

    def updated_json(self):
        return "{}"

    def close(self):
        self.closed = True


class FakeRequest:
    headers = {}
    client = None


class FakeStreamResp:
    """client.stream(...) 上下文里的响应：流式读取接口。"""

    def __init__(self, status_code=200, chunks=(), text=""):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.text = text

    async def aread(self):
        return self.text.encode()

    async def aiter_text(self):
        for c in self._chunks:
            yield c


class ExplodingStreamResp(FakeStreamResp):
    """先吐一个 chunk 再断流 —— 模拟「已 delivered 后连接中断」。"""

    async def aiter_text(self):
        yield self._chunks[0]
        raise RuntimeError("connection reset")


class FakePostResp:
    """client.post(...) 的响应：非流式全量文本。"""

    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _StreamCtx:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *exc):
        return False


def install_fake_httpx(monkeypatch, responses):
    """把 proxy.httpx.AsyncClient 替换为按序弹出 responses 的假客户端。"""
    made = []

    class _Client:
        def __init__(self, *a, **k):
            made.append(self)
            self.requests = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            self.requests.append({"method": method, "url": url,
                                  "headers": headers, "json": json})
            return _StreamCtx(responses.pop(0))

        async def post(self, url, headers=None, json=None):
            self.requests.append({"method": "POST", "url": url,
                                  "headers": headers, "json": json})
            return responses.pop(0)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    return made


def make_selector(accounts):
    """按 exclude_ids 跳过已尝试账号，模拟 _select_account 的换号语义。"""
    calls = []

    def _sel(db, exclude_ids=None, min_balance=1, mark_picked=True, affinity_key=None):
        excl = set(exclude_ids or ())
        calls.append({"exclude_ids": set(excl), "min_balance": min_balance,
                      "affinity_key": affinity_key})
        for a in accounts:
            if a.id not in excl:
                return a
        return None

    _sel.calls = calls
    return _sel


def chat_consumer():
    """Chat SSE 式 consume：chunk 原样转发。"""
    async def consume(r, att):
        async for chunk in r.aiter_text():
            att.mark_ttfb()
            att.delivered = True
            att.usage_parts.append(chunk)
            yield chunk
    return consume


def nonstream_consumer():
    """非流式 consume：聚合行数与 usage 文本，不 yield（骨架零输出）。"""
    async def consume(r, att):
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        att.usage_join = "\n"
        att.usage_parts.extend(lines)
        att.result = {"lines": len(lines)}
        return
        yield  # pragma: no cover
    return consume


def rec_emitters():
    rec = {"client_error": [], "exhausted": []}

    def emit_client_error(status, text):
        rec["client_error"].append((status, text))
        return f"ERR:{status}"

    def emit_exhausted(err_kind, has_err):
        rec["exhausted"].append((err_kind, has_err))
        return f"EXH:{err_kind}"

    return rec, emit_client_error, emit_exhausted


def base_kwargs(make_consumer, emit_client_error, emit_exhausted,
                out=None, upstream_stream=True):
    return dict(
        key_id=1, request=FakeRequest(),
        chat_body={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        order=["glm-5.2"], url="http://upstream.test/v2/chat/completions",
        use_case="chat-completion", mode_label="stream", initial_model="glm-5.2",
        upstream_stream=upstream_stream,
        make_consumer=make_consumer, emit_client_error=emit_client_error,
        emit_exhausted=emit_exhausted, out=out,
    )


def run_loop(**kw):
    async def _run():
        pieces = []
        async for p in proxy._proxy_loop(**kw):
            pieces.append(p)
        return pieces
    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 通用夹具：DB 会话 / 会话工厂 / 记账 全部替换，避免触碰真实 sqlite
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(proxy, "SessionLocal", lambda: db)
    monkeypatch.setattr(proxy, "_account_session_safe",
                        lambda d, acc: FakeSess(acc))
    usage_calls = []
    monkeypatch.setattr(
        proxy, "_record_usage",
        lambda *a, **k: usage_calls.append({"args": a, "kwargs": k}) or 101)
    return {"db": db, "usage_calls": usage_calls}


SSE_CHUNKS = [
    'data: {"id":"c1","model":"glm-5.2","choices":[{"delta":{"content":"hi"}}]}\n\n',
    'data: {"id":"c1","model":"glm-5.2","choices":[{"delta":{"content":"!"}}]}\n\n',
    'data: {"id":"c1","choices":[],"usage":{"credits":"x 12","prompt_tokens":10,'
    '"completion_tokens":5,"total_tokens":15}}\n\ndata: [DONE]\n\n',
]


# ---------------------------------------------------------------------------
# 流式骨架
# ---------------------------------------------------------------------------

def test_stream_success_clears_strikes(_patch_db, monkeypatch):
    """成功：chunk 全量转发、三振计数清零、记账带 token 明细。"""
    acc = FakeAcc(1)
    acc.err_count = 2  # 预置连续失败，成功后必须清零
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc]))
    responses = [FakeStreamResp(200, chunks=SSE_CHUNKS)]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    # 诊断 spy：骨架吞异常换号时把原文带出来
    seen = []
    orig_policy = proxy._apply_account_policy

    def spy_policy(db, a, kind, status, msg):
        seen.append((kind, status, msg))
        return orig_policy(db, a, kind, status, msg)

    monkeypatch.setattr(proxy, "_apply_account_policy", spy_policy)

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))
    if seen:
        print("POLICY CALLS:", seen)
    assert seen == [], f"成功路径不应触发错误策略: {seen}"
    assert pieces == SSE_CHUNKS
    assert acc.err_count == 0
    assert acc.last_used_at is not None
    assert rec["exhausted"] == [] and rec["client_error"] == []
    call = _patch_db["usage_calls"][0]
    # _record_usage(key_id, account_id, model, credits, updated_auth_json, **kw)
    assert call["args"][0] == 1  # key_id
    assert call["args"][1] == 1  # account_id
    assert call["args"][3] == 12.0  # credits（位置参数）
    kw = call["kwargs"]
    assert kw["total_tokens"] == 15
    assert kw["prompt_tokens"] == 10 and kw["completion_tokens"] == 5
    assert kw["use_case"] == "chat-completion" and kw["error_kind"] == "success"
    assert kw["ttfb_ms"] is not None and kw["latency_ms"] is not None


def test_retry_on_429_switches_account(_patch_db, monkeypatch):
    """429 走 soft_rate 冷却并换下一个账号；新账号成功。"""
    acc1, acc2 = FakeAcc(1), FakeAcc(2)
    sel = make_selector([acc1, acc2])
    monkeypatch.setattr(proxy, "_select_account", sel)
    responses = [FakeStreamResp(429, text="rate limited"),
                 FakeStreamResp(200, chunks=SSE_CHUNKS)]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == SSE_CHUNKS
    assert [c["exclude_ids"] for c in sel.calls] == [set(), {1}]
    assert acc1.cool_kind == "soft_rate" and acc1.cool_until is not None
    assert acc2.err_count == 0
    assert rec["exhausted"] == [] and rec["client_error"] == []


def test_session_dead_strikeout_then_success(_patch_db, monkeypatch):
    """session 死亡三振联动：连续第 3 次 → 禁用 + 计数清零，然后换号成功。"""
    acc1, acc2 = FakeAcc(1), FakeAcc(2)
    acc1.err_count = 2
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc1, acc2]))
    responses = [FakeStreamResp(500, text="Offline user session not found"),
                 FakeStreamResp(200, chunks=SSE_CHUNKS)]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == SSE_CHUNKS
    assert acc1.status == "disabled" and acc1.err_count == 0
    assert acc2.err_count == 0
    assert rec["exhausted"] == []


def test_non_retryable_400_stops(_patch_db, monkeypatch):
    """400 client 类错误：透传 emit_client_error 输出，不换号、不禁用。"""
    acc = FakeAcc(1)
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc]))
    responses = [FakeStreamResp(400, text="bad request content")]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == ["ERR:400"]
    assert rec["client_error"] == [(400, "bad request content")]
    assert rec["exhausted"] == []
    assert acc.status == "active"
    assert acc.last_err_msg == "bad request content"


def test_delivered_aborts_on_stream_error(_patch_db, monkeypatch):
    """已向客户端吐过内容后断流：只能中止，不换号（避免内容重复）。"""
    acc1, acc2 = FakeAcc(1), FakeAcc(2)
    sel = make_selector([acc1, acc2])
    monkeypatch.setattr(proxy, "_select_account", sel)
    responses = [ExplodingStreamResp(200, chunks=["first-chunk\n\n"])]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == ["first-chunk\n\n"]
    assert len(sel.calls) == 1  # 没有换号
    assert rec["exhausted"] == [] and rec["client_error"] == []


def test_exhausted_after_all_servers(_patch_db, monkeypatch):
    """全部 5xx：三账号依次尝试，最终 emit_exhausted('server')。"""
    accs = [FakeAcc(i) for i in (1, 2, 3)]
    sel = make_selector(accs)
    monkeypatch.setattr(proxy, "_select_account", sel)
    responses = [FakeStreamResp(500, text="boom") for _ in accs]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == ["EXH:server"]
    assert rec["exhausted"] == [("server", True)]
    assert len(sel.calls) == 3
    for a in accs:
        assert a.err_count == 1  # 5xx 累计一次
        assert a.last_err_msg == "boom"
    # 失败路径也要留记账痕迹（account_id=0）
    last = _patch_db["usage_calls"][-1]
    assert last["args"][1] == 0 and last["kwargs"]["error_kind"] == "server"


def test_no_account_reports_no_account(_patch_db, monkeypatch):
    """号池为空：emit_exhausted('no_account', has_err=False)。"""
    monkeypatch.setattr(proxy, "_select_account", make_selector([]))
    responses = []
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    pieces = run_loop(**base_kwargs(chat_consumer, ece, eex))

    assert pieces == ["EXH:no_account"]
    assert rec["exhausted"] == [("no_account", False)]


def test_model_fallback_rebuilds_body_per_model(_patch_db, monkeypatch):
    """多候选模型：每个模型重建请求体且 model 字段正确、换号按模型重置。"""
    acc1, acc2 = FakeAcc(1), FakeAcc(2)
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc1, acc2]))
    responses = [FakeStreamResp(429, text="rl"),   # 模型A-账号1
                 FakeStreamResp(429, text="rl"),   # 模型A-账号2
                 FakeStreamResp(200, chunks=SSE_CHUNKS)]  # 模型B-账号1
    clients = install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()

    kw = base_kwargs(chat_consumer, ece, eex)
    kw["order"] = ["model-a", "model-b"]
    pieces = run_loop(**kw)

    assert pieces == SSE_CHUNKS
    # 整个循环共享 1 个 AsyncClient（重构后的统一行为），3 次请求都记录在它的 requests 里
    models = [req["json"]["model"] for c in clients for req in c.requests]
    assert models == ["model-a", "model-a", "model-b"]


# ---------------------------------------------------------------------------
# 非流式骨架
# ---------------------------------------------------------------------------

def test_nonstream_success_uses_outcome(_patch_db, monkeypatch):
    """非流式成功：结果进 out 容器，骨架零 yield。"""
    acc = FakeAcc(1)
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc]))
    body_text = ('data: {"id":"c1","choices":[],"usage":{"credits":"x 3",'
                 '"total_tokens":9}}\n\ndata: [DONE]\n\n')
    responses = [FakePostResp(200, text=body_text)]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()
    out = proxy._ProxyOutcome()

    kw = base_kwargs(nonstream_consumer, ece, eex, out=out, upstream_stream=False)
    kw["mode_label"] = "resp"
    kw["use_case"] = "responses"
    pieces = run_loop(**kw)

    assert pieces == []  # 非流式骨架不对外 yield
    assert out.response == {"lines": 2}
    call = _patch_db["usage_calls"][0]
    assert call["args"][3] == 3.0  # credits（位置参数）
    assert call["kwargs"]["use_case"] == "responses"
    assert rec["exhausted"] == [] and rec["client_error"] == []


def test_nonstream_client_error_uses_outcome(_patch_db, monkeypatch):
    """非流式 400：emit 把 JSONResponse 写进 out，状态码与文案保留。"""
    rec = {"client_error": [], "exhausted": []}
    out = proxy._ProxyOutcome()

    def emit_client_error(status, text):
        rec["client_error"].append((status, text))
        # 模拟真实非流式端点：把 JSONResponse 写进 out，不返回数据块
        out.response = proxy.JSONResponse(status_code=status,
                                          content={"error": {"message": text}})
        return None

    def emit_exhausted(err_kind, has_err):
        rec["exhausted"].append((err_kind, has_err))
        out.response = proxy.JSONResponse(status_code=503,
                                          content={"error": {"message": "exhausted"}})
        return None

    monkeypatch.setattr(proxy, "_select_account", make_selector([FakeAcc(1)]))
    responses = [FakePostResp(400, text="nope")]
    install_fake_httpx(monkeypatch, responses)

    kw = base_kwargs(nonstream_consumer, emit_client_error, emit_exhausted,
                     out=out, upstream_stream=False)
    pieces = run_loop(**kw)

    assert pieces == []
    assert out.response.status_code == 400
    assert json.loads(out.response.body)["error"]["message"] == "nope"
    assert rec["client_error"] == [(400, "nope")]


def test_nonstream_retry_switches_account(_patch_db, monkeypatch):
    """非流式 429 → 换号 → 成功聚合。"""
    acc1, acc2 = FakeAcc(1), FakeAcc(2)
    sel = make_selector([acc1, acc2])
    monkeypatch.setattr(proxy, "_select_account", sel)
    body_text = 'data: {"id":"c1","choices":[],"usage":{"total_tokens":7}}\n\n'
    responses = [FakePostResp(429, text="rl"), FakePostResp(200, text=body_text)]
    install_fake_httpx(monkeypatch, responses)
    rec, ece, eex = rec_emitters()
    out = proxy._ProxyOutcome()

    kw = base_kwargs(nonstream_consumer, ece, eex, out=out, upstream_stream=False)
    pieces = run_loop(**kw)

    assert pieces == []
    assert out.response == {"lines": 1}
    assert len(sel.calls) == 2
    assert acc1.cool_kind == "soft_rate"


# ---------------------------------------------------------------------------
# Anthropic 端点的思考透传（响应侧）—— 走真实 make_consumer 接线
#
# 复刻 proxy.py /v1/messages 流式路径的 consume 回调，验证上游的
# reasoning_content 真的会被转成 Anthropic 的 thinking 块发出去。
# ---------------------------------------------------------------------------

class FakeLineStreamResp(FakeStreamResp):
    """按行读取的假上游响应（Anthropic 路径用 aiter_lines）。"""

    async def aiter_lines(self):
        for c in self._chunks:
            for ln in c.splitlines():
                yield ln


ANTHROPIC_REASONING_CHUNKS = [
    'data: {"model":"deepseek-v4.1-flash","choices":[{"delta":{"reasoning_content":"让我想想"}}]}\n\n',
    'data: {"model":"deepseek-v4.1-flash","choices":[{"delta":{"content":"答案"}}]}\n\n',
    'data: {"model":"deepseek-v4.1-flash","choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
    'data: [DONE]\n\n',
]


def anthropic_consumer():
    """与 proxy.py 的 /v1/messages 流式 consume 同形。"""
    from core.anthropic_adapter import AnthropicStreamConverter

    async def consume(r, att):
        conv = AnthropicStreamConverter(model="deepseek-v4.1-flash")
        att.usage_join = "\n"
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            att.mark_ttfb()
            att.usage_parts.append(line)
            events = conv.feed_line(line)
            if events:
                att.delivered = True
                yield events
        tail = conv.finish()
        if tail:
            yield tail
    return consume


def test_anthropic_path_emits_thinking_block(_patch_db, monkeypatch):
    """上游 reasoning_content → 客户端收到 thinking_delta（修复前一个都没有）。"""
    acc = FakeAcc(1)
    monkeypatch.setattr(proxy, "_select_account", make_selector([acc]))
    install_fake_httpx(monkeypatch,
                       [FakeLineStreamResp(200, chunks=ANTHROPIC_REASONING_CHUNKS)])
    rec, ece, eex = rec_emitters()

    kw = base_kwargs(anthropic_consumer, ece, eex)
    kw.update(order=["deepseek-v4.1-flash"], initial_model="deepseek-v4.1-flash")
    blob = "".join(run_loop(**kw))

    assert "thinking_delta" in blob, "修复后必须有思考事件"
    assert "signature_delta" in blob, "thinking 块要带 signature"
    assert blob.index('"type": "thinking"') < blob.index('"type": "text"')
    assert rec["client_error"] == [] and rec["exhausted"] == []
