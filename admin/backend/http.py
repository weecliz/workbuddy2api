"""上游 HTTP 公共参数与凭据元信息解析。

独立成模块的原因：HTTP_LIMITS 被 proxy / models 两个路由直接复用，
parse_auth_meta 被 OAuth 加号与账号导入复用，都与「会话」无关。
"""
import json

import httpx

# 连接池：减少 TLS 握手，与 Go 项目 MaxIdleConnsPerHost=20 对齐。
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

# growth 域路径前缀（猫猫旅行 / 连登 / 领养），实际完整路径带 /v2 前缀。
_GROWTH_BASE = "/v2/activity/growth"


def parse_auth_meta(auth_json: str) -> dict:
    """从 .info 原文里抽取账号元信息（uid / enterpriseId / domain / 昵称）。"""
    try:
        data = json.loads(auth_json)
    except Exception:
        return {}
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    return {
        "uid": str(acct.get("uid") or ""),
        "enterprise_id": str(acct.get("enterpriseId") or ""),
        "domain": str(auth.get("domain") or ""),
        "nickname": str(acct.get("nickname") or ""),
    }
