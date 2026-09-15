"""单账号上游会话（按域拆分：http / session / checkin / growth）。

原来是一个 271 行的 backend.py，五个域混在一起；现在按职责拆成四个模块，
**对外 API 完全不变** —— 调用方照旧写：

    from admin import backend
    backend.AccountSession(auth_json)      # 会话
    backend.HTTP_LIMITS                    # 连接池参数
    backend.parse_auth_meta(auth_json)     # 凭据元信息
    backend.CredentialManager              # 底层鉴权（proxy 读倍率用）

各模块职责：
    http.py     连接池参数、growth 路径前缀、凭据元信息解析
    session.py  AccountSession：凭据落盘/回写 + 档案与额度（转发签到/成长/用量）
    checkin.py  每日签到：状态查询与领取
    growth.py   成长中心：猫猫领养 / 旅行 / 连登 / 对话活跃上报
    usage.py    上游请求用量：区间全量拉取 + 汇总/按天/按模型聚合
"""
from core.converter import CredentialManager

from .http import HTTP_LIMITS, parse_auth_meta
from .session import AccountSession
from .usage import (
    aggregate_records,
    fetch_usage_range,
    merge_aggregates,
    normalize_range,
)

__all__ = [
    "AccountSession",
    "HTTP_LIMITS",
    "parse_auth_meta",
    "CredentialManager",
    "fetch_usage_range",
    "aggregate_records",
    "merge_aggregates",
    "normalize_range",
]
