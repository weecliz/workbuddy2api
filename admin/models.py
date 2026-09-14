"""ORM 模型：账号、API Key、用量日志。"""
from datetime import datetime

from sqlalchemy import CLOB, Column, DateTime, Float, Integer, String, Text

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, default="")
    uid = Column(String(120), default="")
    enterprise_id = Column(String(120), default="")
    domain = Column(String(120), default="")
    auth_json = Column(TextColumn, nullable=False)  # 原始 .info 内容（含 token）
    status = Column(String(16), default="active")  # active | disabled
    balance_total = Column(Integer, default=0)
    balance_remain = Column(Integer, default=0)
    # 成长中心连登天数（GET /v2/activity/growth/streak，刷新余额时顺带同步）
    streak_days = Column(Integer, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    # 稳定性状态机：错误计数 / 冷却 / 防撞号 / 禁用原因
    err_count = Column(Integer, default=0)          # 连续上游 5xx 计数
    cool_until = Column(DateTime, nullable=True)    # 冷却截止时间
    cool_kind = Column(String(16), default="")       # hard_credit | soft_rate | error_threshold | not_found
    last_err_at = Column(DateTime, nullable=True)
    last_err_msg = Column(String(255), default="")
    last_picked_at = Column(DateTime, nullable=True)  # 最近一次被选中，用于 100ms 防撞号窗口
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    """对外共享的 API Key，带积分限额。"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    key_hash = Column(String(128), unique=True, nullable=False)
    key_prefix = Column(String(16), default="")  # 展示用前缀
    key_full = Column(KeyFullColumn, default="")  # 完整密钥（仅管理后台查看用，base64 编码存储）
    credit_limit = Column(Float, default=0)  # 限额（credits）；unlimited=True 时忽略
    credit_used = Column(Float, default=0)
    unlimited = Column(Integer, default=0)  # 0/1
    status = Column(String(16), default="active")  # active | revoked
    note = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UsageLog(Base):
    """每次代理调用的用量记录（用于管理后台审计）。

    记录内容：调用方 API Key、实际使用的上游账号、模型、积分消耗、token 明细，
    以及发起请求的真实客户端 IP（来自 X-Forwarded-For / X-Real-IP / 直连 socket），
    便于风控对账与上游客用途日志对齐。
    """

    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, nullable=False, default=0)
    account_id = Column(Integer, nullable=False, default=0)
    model = Column(String(120), default="")
    credits = Column(Float, default=0)
    # 详细用量：优先取上游 usage 字段；缺省为 NULL
    prompt_tokens = Column(Integer, nullable=True, default=None)
    completion_tokens = Column(Integer, nullable=True, default=None)
    total_tokens = Column(Integer, nullable=True, default=None)
    cached_tokens = Column(Integer, nullable=True, default=None)
    # 发起请求的真实客户端 IP（经反代时取 X-Forwarded-For 首个，否则 X-Real-IP / 直连 IP）
    client_ip = Column(String(64), default="")
    # 用途标识（透传给上游的 X-Agent-Purpose），便于风控审计与上游请求用量对齐
    use_case = Column(String(64), default="")
    # 请求级表格日志字段（ logging）：TTFB / 总耗时 / 序号 / 错误分类
    seq = Column(Integer, default=0)
    ttfb_ms = Column(Integer, nullable=True, default=None)
    latency_ms = Column(Integer, nullable=True, default=None)
    error_kind = Column(String(32), default="")  # hard_credit | soft_rate | server | not_found | session_dead | transport | client | success
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelConfig(Base):
    """模型白名单配置（系统级 / 用户级）。"""

    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), default="system")  # system | user
    model_id = Column(String(120), nullable=False)  # 模型 ID，如 "deepseek-v4-flash"
    enabled = Column(Integer, default=1)  # 0/1
    note = Column(String(255), default="")
    credit_multiplier = Column(Float, default=0)  # 积分消耗倍率；0=免费模型
    credits_raw = Column(String(120), default="")  # 原始 credits 字符串（如 "x0.05" / "x0.00 credits"）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemSetting(Base):
    """简单的键值配置（同步地址 / 密钥 / 其他开关）。"""

    __tablename__ = "system_settings"

    key = Column(String(120), primary_key=True)
    value = Column(TextColumn, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Schedule(Base):
    """后台定时任务（如：整点刷新平台总积分、每日同步模型列表）。"""

    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    task = Column(String(40), default="refresh_balances")  # refresh_balances | sync_models | daily_checkin
    interval_minutes = Column(Integer, default=60)
    enabled = Column(Integer, default=1)  # 0/1
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    last_result = Column(TextColumn, default="")  # 上次运行结果摘要
    # 停止领取时间（仅 daily_checkin 任务使用）：到达该时间后不再执行领取请求，
    # 避免活动下线后继续请求触发上游风控。可由活动 end_time 预填或运行中发现 EventEnded 自动写入。
    stop_after = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
