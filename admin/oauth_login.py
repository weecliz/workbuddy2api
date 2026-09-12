"""OAuth 设备授权登录：在管理后台直接添加账号，不需要桌面端参与。

流程（无 PKCE，state 由服务端签发；与官方 CodeBuddy CLI / WorkBuddy 桌面端同一套，
两端 product.json 的 platform 都是 "CLI"、prefixPath 都是 "/plugin"）：

  ① POST {backend}/v2/plugin/auth/state?platform=CLI
        -> {state, authUrl}
  ② 人工在浏览器打开 authUrl 完成登录
  ③ GET  {backend}/v2/plugin/auth/token?state=<state>
        -> {accessToken, refreshToken, expiresIn, domain}    未登录完成时业务 code != 0
  ④ GET  {backend}/v2/plugin/login/account?state=<state>      带 Bearer
        -> {uid, enterpriseId, nickname}

产出的账号 JSON 与桌面端 `.info` 结构一致（auth / account 嵌套形），
因此可直接交给 `admin.backend.parse_auth_meta` 解析入库，无需改动既有导入逻辑。

安全设计（重要）：
  - **真实 state 只在服务端内存里，不下发浏览器**。对外只暴露一次性的 `login_id`。
    state 本身就是凭据（谁拿到谁能 poll 出 token），绝不能出现在前端或日志里。
  - 三个入口都要求管理员登录态（`require_admin`）。
  - 条目默认 10 分钟过期；`take_credentials()` 领取后立即删除，token 不长期驻留内存。
  - 每个登录流程独立 httpx.Client（自带 cookie jar），多账号登录互不串会话。
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time

import httpx

_LOGGER = logging.getLogger(__name__)

# 上游基址（与 admin.config.settings.BACKEND 一致，此处独立读取避免循环依赖）
_BACKEND = (os.getenv("ADMIN_BACKEND") or "https://copilot.tencent.com").rstrip("/")
_PREFIX = "/v2/plugin"

# platform 取值：官方 CLI 与桌面端 product.json 里都是 "CLI"（桌面端只改了 productName）
_PLATFORM = os.getenv("ADMIN_OAUTH_PLATFORM", "CLI")

# 出站 UA。官方格式为 `{platform}/{platformVersion} {productName}/{productVersion}`，
# 取值随客户端版本变化，故做成可配置；默认按本机实测的 CodeBuddy CLI 版本。
# 若走桌面端身份，可改为 CLI/<ver> WorkBuddy/<ver>。
_UA = os.getenv("ADMIN_OAUTH_USER_AGENT", "CLI/2.148.0 CodeBuddy/2.148.0")
_ORIGIN = os.getenv("ADMIN_OAUTH_ORIGIN", "https://www.codebuddy.cn")

# 登录会话存活时间（秒）。超时后 state 作废、条目回收。
_TTL = float(os.getenv("ADMIN_OAUTH_TTL", "600"))
_TIMEOUT = float(os.getenv("ADMIN_OAUTH_TIMEOUT", "15"))

# 登录响应不带 refreshExpiresIn，按桌面端 .info 实测常量兜底（90 天）。
# 注意：该值仅用于把文件写得与真实 .info 一致；后续每次 refresh 都会重算，不影响鉴权。
_DEFAULT_REFRESH_EXPIRES_IN = 7_776_000

# 上游「登录尚未完成」的业务码。这两个常量来自官方客户端实现
# （`LOGIN_TOKEN_PENDING_CODE` / `LOGIN_ACCOUNT_PENDING_CODE`）：
#   - 只有恰好等于该码才算 pending，**其余非 0 码都是致命错误**；
#   - 若一开始就把「任何非 0」当 pending，真实错误会退化成「永远在等待」，直到超时才暴露。
_PENDING_TOKEN_CODE = 11217
_PENDING_ACCOUNT_CODE = 12151

# /login/account 的等待参数：官方同样是轮询该端点直到拿到账号信息。
_ACCOUNT_RETRY = int(os.getenv("ADMIN_OAUTH_ACCOUNT_RETRY", "5"))
_ACCOUNT_RETRY_INTERVAL = float(os.getenv("ADMIN_OAUTH_ACCOUNT_INTERVAL", "1"))

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _headers(extra: dict | None = None) -> dict:
    """与官方客户端一致的通用请求头。

    说明：`X-No-*` 系列仅在「字段缺失」的请求上发送，且官方取值是字符串 "true"
    （不是 "1"）。这里按官方实现分别用于 auth/token 与 login/account。
    """
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
        "User-Agent": _UA,
    }
    if extra:
        h.update(extra)
    return h


_NO_AUTH_HEADERS = {
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
}

_NO_ENTITY_HEADERS = {
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
}


def _unwrap(resp: httpx.Response) -> dict:
    """解析 {code, msg, data} 信封。HTTP 非 2xx 或非 0 code 抛 RuntimeError。"""
    try:
        env = resp.json()
    except Exception:
        raise RuntimeError(f"上游返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"上游 HTTP {resp.status_code}：{env.get('msg') or resp.text[:200]}")
    if not isinstance(env, dict):
        raise RuntimeError("上游返回结构异常（非对象）")
    if env.get("code") not in (0, None):
        raise RuntimeError(f"上游业务码 {env.get('code')}：{env.get('msg')}")
    return env.get("data") or {}


def _gc_locked(now: float) -> None:
    """回收过期会话（调用方必须已持锁）。"""
    dead = [k for k, v in _sessions.items() if now - v["created_at"] > _TTL]
    for k in dead:
        ent = _sessions.pop(k, None)
        if ent:
            try:
                ent["client"].close()
            except Exception:
                pass


def _get(login_id: str) -> dict | None:
    now = time.time()
    with _lock:
        _gc_locked(now)
        return _sessions.get(login_id)


def _drop(login_id: str) -> None:
    with _lock:
        ent = _sessions.pop(login_id, None)
    if ent:
        try:
            ent["client"].close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 四个步骤
# ---------------------------------------------------------------------------

def start_login() -> dict:
    """步骤①②：申请 state + authUrl，登记一个待完成的登录会话。

    返回 {login_id, auth_url, expires_in}；其中 login_id 是给前端的句柄，
    真实 state 保留在服务端。
    """
    url = f"{_BACKEND}{_PREFIX}/auth/state?platform={_PLATFORM}"
    client = httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
    try:
        r = client.post(url, json={}, headers=_headers())
        data = _unwrap(r)
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise

    state = str(data.get("state") or "").strip()
    auth_url = str(data.get("authUrl") or "").strip()
    if not state or not auth_url:
        try:
            client.close()
        except Exception:
            pass
        raise RuntimeError("上游未返回 state / authUrl，无法发起授权")

    login_id = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _gc_locked(now)
        _sessions[login_id] = {
            "state": state,
            "auth_url": auth_url,
            "client": client,
            "created_at": now,
            "status": "pending",
            "payload": None,
            "error": "",
        }
    _LOGGER.info("OAuth 登录会话已创建 platform=%s（state 不下发前端）", _PLATFORM)
    return {"login_id": login_id, "auth_url": auth_url, "expires_in": int(_TTL)}


def _read_code(env) -> int | None:
    """读信封里的业务码。对齐官方 `readLoginEnvelopeCode`：兼容 code 与 response.code 两处。"""
    if not isinstance(env, dict):
        return None
    code = env.get("code")
    if isinstance(code, int):
        return code
    resp = env.get("response")
    if isinstance(resp, dict):
        inner = resp.get("data")
        cand = inner.get("code") if isinstance(inner, dict) else None
        if cand is None:
            cand = resp.get("code")
        if isinstance(cand, int):
            return cand
    return None


def _poll_token(client: httpx.Client, state: str) -> dict | None:
    """步骤③：取 token。

    返回 token 字典；返回 None 表示**登录尚未完成**（仅有 `code == 11217` 这一种情况）。
    其他业务码 / 结构异常一律抛 RuntimeError —— 由调用方把会话标记为 error 并回传前端，
    不能笼统当成 pending，否则真实错误会伪装成「一直在等待」。

    判定顺序对齐官方：**先看有没有 accessToken，再看 pending 码**。
    """
    url = f"{_BACKEND}{_PREFIX}/auth/token?state={state}"
    r = client.get(url, headers=_headers(_NO_AUTH_HEADERS))
    try:
        env = r.json()
    except Exception:
        raise RuntimeError(f"token 端点返回非 JSON（HTTP {r.status_code}）：{r.text[:160]}")
    if not isinstance(env, dict):
        raise RuntimeError("token 端点返回结构异常（非对象）")

    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    token = str(data.get("accessToken") or "").strip()
    if token:                                   # 成功只看有没有 accessToken
        return data

    code = _read_code(env)
    if code == _PENDING_TOKEN_CODE:
        _LOGGER.debug("OAuth 尚未完成登录（code=%s msg=%s）", code, env.get("msg"))
        return None
    raise RuntimeError(f"上游返回业务码 {code}（{env.get('msg') or '无 msg'}），无法继续等待登录")


def _fetch_account(client: httpx.Client, state: str, access_token: str) -> dict:
    """步骤④：拿 uid / enterpriseId / nickname。

    官方同样轮询该端点（pending 码 12151），所以这里也重试若干次。
    与 token 不同：拿不到账号信息不影响已经到手的凭据，因此最终失败只告警并返回空 dict，
    由调用方兜底（名称回落为 uid / 未命名，可事后在号池里改）。
    """
    url = f"{_BACKEND}{_PREFIX}/login/account?state={state}"
    headers = _headers({**_NO_ENTITY_HEADERS, "Authorization": f"Bearer {access_token}"})
    for attempt in range(max(1, _ACCOUNT_RETRY)):
        if attempt:
            time.sleep(_ACCOUNT_RETRY_INTERVAL)
        try:
            r = client.get(url, headers=headers)
        except Exception as exc:
            _LOGGER.warning("OAuth 账号信息请求失败（第 %d 次）：%s", attempt + 1, exc)
            continue
        # 401/403 是网关层直接拒了（实测为 openresty 的 HTML 401），重试无意义
        if r.status_code in (401, 403):
            _LOGGER.warning("OAuth 账号信息被拒（HTTP %d），不再重试", r.status_code)
            break
        try:
            env = r.json()
        except Exception:
            _LOGGER.warning("OAuth 账号信息返回非 JSON（HTTP %d）", r.status_code)
            continue
        if not isinstance(env, dict):
            continue
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        if data.get("uid"):
            return data
        code = _read_code(env)
        if code != _PENDING_ACCOUNT_CODE:
            _LOGGER.warning("OAuth 账号信息返回业务码 %s：%s", code, env.get("msg"))
            break
    _LOGGER.warning("OAuth 账号信息未就绪，uid 将为空（凭据本身可用，可在号池里手动补名称）")
    return {}


def poll_login(login_id: str) -> dict:
    """查询登录进度。

    - 尚未完成：{"status": "pending"}      —— 前端继续轮询即可
    - 已完成：  {"status": "ready", "account": {...meta...}}
    - 出错：    {"status": "error", "msg": "..."}   —— 会话终结，前端应停止轮询并提示重新发起
    - 会话不存在/过期：KeyError（路由层转 404）

    **只返回账号元信息，不返回任何 token。**
    """
    ent = _get(login_id)
    if ent is None:
        raise KeyError(login_id)
    if ent["status"] == "ready":
        return {"status": "ready", "account": _public_meta(ent["payload"])}
    if ent["status"] == "error":
        return {"status": "error", "msg": ent.get("error") or "授权失败"}

    client: httpx.Client = ent["client"]
    try:
        token = _poll_token(client, ent["state"])
    except Exception as exc:
        # 非 pending 的失败：会话立即终结，把原因带给前端，避免假装还在等待
        ent["status"] = "error"
        ent["error"] = str(exc)
        _LOGGER.warning("OAuth 轮询失败，会话已标记 error：%s", exc)
        return {"status": "error", "msg": str(exc)}
    if token is None:
        return {"status": "pending"}

    account = _fetch_account(client, ent["state"], token["accessToken"])
    ent["payload"] = {"token": token, "account": account}
    ent["status"] = "ready"
    _LOGGER.info(
        "OAuth 登录完成 uid=%s nickname=%s",
        account.get("uid") or "-", account.get("nickname") or "-",
    )
    return {"status": "ready", "account": _public_meta(ent["payload"])}


def _public_meta(payload: dict) -> dict:
    token = payload.get("token") or {}
    account = payload.get("account") or {}
    return {
        "nickname": str(account.get("nickname") or ""),
        "uid": str(account.get("uid") or ""),
        "enterprise_id": str(account.get("enterpriseId") or ""),
        "domain": str(token.get("domain") or ""),
        "expires_in": int(token.get("expiresIn") or 0),
    }


def build_auth_json(payload: dict) -> str:
    """把 OAuth 结果组装成与桌面端 `.info` 一致的 JSON（供 parse_auth_meta 解析）。"""
    token = payload.get("token") or {}
    account = payload.get("account") or {}
    now_ms = int(time.time() * 1000)

    try:
        expires_in = int(token.get("expiresIn") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    try:
        refresh_expires_in = int(token.get("refreshExpiresIn") or 0)
    except (TypeError, ValueError):
        refresh_expires_in = 0
    if refresh_expires_in <= 0:
        refresh_expires_in = _DEFAULT_REFRESH_EXPIRES_IN

    auth: dict = {
        "accessToken": token.get("accessToken") or "",
        "refreshToken": token.get("refreshToken") or "",
        "tokenType": "Bearer",
        "domain": token.get("domain") or "",
        "lastRefreshTime": now_ms,
    }
    if expires_in > 0:
        auth["expiresIn"] = expires_in
        auth["expiresAt"] = now_ms + expires_in * 1000
    auth["refreshExpiresIn"] = refresh_expires_in
    auth["refreshExpiresAt"] = now_ms + refresh_expires_in * 1000

    doc = {
        "auth": auth,
        "account": {
            "uid": str(account.get("uid") or ""),
            "enterpriseId": str(account.get("enterpriseId") or ""),
            "nickname": str(account.get("nickname") or ""),
        },
    }
    import json

    return json.dumps(doc, ensure_ascii=False, indent=2)


def take_credentials(login_id: str) -> dict:
    """领取并删除会话，返回 {"auth_json": str, "meta": dict}。一次性使用。"""
    ent = _get(login_id)
    if ent is None:
        raise KeyError(login_id)
    if ent["status"] == "error":
        raise RuntimeError(f"授权已失败：{ent.get('error') or '未知原因'}")
    if ent["status"] != "ready" or not ent.get("payload"):
        raise RuntimeError("登录尚未完成，无法领取凭据")
    payload = ent["payload"]
    auth_json = build_auth_json(payload)
    meta = _public_meta(payload)
    _drop(login_id)  # 领取即销毁，token 不再驻留内存
    return {"auth_json": auth_json, "meta": meta}


def pending_count() -> int:
    """当前未完成的登录会话数（供状态展示 / 排查）。"""
    now = time.time()
    with _lock:
        _gc_locked(now)
        return sum(1 for v in _sessions.values() if v["status"] == "pending")
