"""ORM 模型：账号、API Key、用量日志。

用 SQLAlchemy 2.0 的 ``Mapped[...]`` + ``mapped_column()`` 声明式写法，
而不是旧式 ``Column()``：后者的类属性在类型层面是 ``Column[X]``，
类型检查器（pyright）会把每一处 ``obj.attr = <X>`` 与 ``obj.attr`` 都判为
"X 不能赋给 Column[X]"，在本项目里累计出 200+ 条纯噪音，把真问题（例如
未定义名）淹没在其中。

``Mapped[X]`` 让类属性就是 ``X``；**可空性由注解决定**：
    Mapped[int]        -> NOT NULL
    Mapped[int | None] -> NULL

因此下面每个字段的可空性都刻意与既有表结构保持一致（见迁移前的
information_schema 基线），避免只改类型、却把建表 DDL 一起改了。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import CLOB, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from admin.db import Base

# 长文本列的跨方言写法：MySQL 保持原语义，DB2 编译成 CLOB。
#   1) DB2 没有 MySQL 的 TEXT 类型，显式指定更稳；
#   2) key_full 这种 VARCHAR(2048) 在 DB2 默认 4K 页里容易触发「行长超限」(SQLSTATE 54010)，
#      它在业务里只用于后台展示、从不参与查询条件，因此 DB2 上改成 CLOB 最省事。
# ibm_db_sa 同时以 db2 / ibm_db_sa 两个 dialect 名注册，两个都登记。
TextColumn = Text().with_variant(CLOB(), "db2", "ibm_db_sa")
KeyFullColumn = String(2048).with_variant(CLOB(), "db2", "ibm_db_sa")


class Account(Base):
    """一个 WorkBuddy / CodeBuddy 登录态（.info 凭据）。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    uid: Mapped[Optional[str]] = mapped_column(String(120), default="")
    enterprise_id: Mapped[Optional[str]] = mapped_column(String(120), default="")
    domain: Mapped[Optional[str]] = mapped_column(String(120), default="")
    # 原始 .info 内容（含 token）
    auth_json: Mapped[str] = mapped_column(TextColumn, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(16), default="active")  # active | disabled
    balance_total: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    balance_remain: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 成长中心连登天数（GET /v2/activity/growth/streak，刷新余额时顺带同步）
    streak_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 稳定性状态机：错误计数 / 冷却 / 防撞号 / 禁用原因
    err_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)  # 连续上游 5xx 计数
    cool_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 冷却截止时间
    # hard_credit | soft_rate | error_threshold | not_found
    cool_kind: Mapped[Optional[str]] = mapped_column(String(16), default="")
    last_err_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_err_msg: Mapped[Optional[str]] = mapped_column(String(255), default="")
    # 最近一次被选中，用于 100ms 防撞号窗口
    last_picked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    """对外共享的 API Key，带积分限额。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), default="")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    key_prefix: Mapped[Optional[str]] = mapped_column(String(16), default="")  # 展示用前缀
    # 完整密钥（仅管理后台查看用，base64 编码存储）
    key_full: Mapped[Optional[str]] = mapped_column(KeyFullColumn, default="")
    # 限额（credits）；unlimited=True 时忽略
    credit_limit: Mapped[Optional[float]] = mapped_column(Float, default=0)
    credit_used: Mapped[Optional[float]] = mapped_column(Float, default=0)
    unlimited: Mapped[Optional[int]] = mapped_column(Integer, default=0)  # 0/1
    status: Mapped[Optional[str]] = mapped_column(String(16), default="active")  # active | revoked
    note: Mapped[Optional[str]] = mapped_column(String(255), default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UsageLog(Base):
    """每次代理调用的用量记录（用于管理后台审计）。

    记录内容：调用方 API Key、实际使用的上游账号、模型、积分消耗、token 明细，
    以及发起请求的真实客户端 IP（来自 X-Forwarded-For / X-Real-IP / 直连 socket），
    便于风控对账与上游客用途日志对齐。
    """

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[Optional[str]] = mapped_column(String(120), default="")
    credits: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 详细用量：优先取上游 usage 字段；缺省为 NULL
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    cached_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    # 发起请求的真实客户端 IP（经反代时取 X-Forwarded-For 首个，否则 X-Real-IP / 直连 IP）
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), default="")
    # 用途标识（透传给上游的 X-Agent-Purpose），便于风控审计与上游请求用量对齐
    use_case: Mapped[Optional[str]] = mapped_column(String(64), default="")
    # 请求级表格日志字段（logging）：TTFB / 总耗时 / 序号 / 错误分类
    seq: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    ttfb_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    # hard_credit | soft_rate | server | not_found | session_dead | transport | client | success
    error_kind: Mapped[Optional[str]] = mapped_column(String(32), default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)


class ModelConfig(Base):
    """模型白名单配置（系统级 / 用户级）。"""

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[Optional[str]] = mapped_column(String(16), default="system")  # system | user
    # 模型 ID，如 "deepseek-v4-flash"
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[Optional[int]] = mapped_column(Integer, default=1)  # 0/1
    note: Mapped[Optional[str]] = mapped_column(String(255), default="")
    # 积分消耗倍率；0=免费模型
    credit_multiplier: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 原始 credits 字符串（如 "x0.05" / "x0.00 credits"）
    credits_raw: Mapped[Optional[str]] = mapped_column(String(120), default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemSetting(Base):
    """简单的键值配置（同步地址 / 密钥 / 其他开关）。"""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(TextColumn, default="")
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Schedule(Base):
    """后台定时任务（如：整点刷新平台总积分、每日同步模型列表）。"""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), default="")
    # refresh_balances | sync_models | daily_checkin
    task: Mapped[Optional[str]] = mapped_column(String(40), default="refresh_balances")
    interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, default=60)
    enabled: Mapped[Optional[int]] = mapped_column(Integer, default=1)  # 0/1
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 上次运行结果摘要
    last_result: Mapped[Optional[str]] = mapped_column(TextColumn, default="")
    # 停止领取时间（仅 daily_checkin 任务使用）：到达该时间后不再执行领取请求，
    # 避免活动下线后继续请求触发上游风控。可由活动 end_time 预填或运行中发现 EventEnded 自动写入。
    stop_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
