"""SQLAlchemy 引擎 / 会话 / Base，并负责建库建表。"""
import re

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from admin.config import settings

# SQL 标识符白名单：仅允许常规字母数字下划线，杜绝任何拼接注入。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法 SQL 标识符: {name!r}")
    return name


def _quote_db_name(name: str) -> str:
    """库名（来自配置）去除反引号转义后安全引用。"""
    return name.replace("`", "").replace("\\", "").strip()

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_recycle=3600,
                        pool_timeout=30, pool_size=20, max_overflow=40, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_database():
    """若 DATABASE_URL 指向的库不存在则创建（仅 MySQL）。"""
    url = settings.DATABASE_URL
    if not url.startswith("mysql"):
        return
    db_name = url.split("/")[-1].split("?")[0]
    rest = url.split("://", 1)[1]
    user_pass, host_rest = rest.split("@", 1)
    user, pwd = (user_pass.split(":", 1) + [""])[:2] if ":" in user_pass else (user_pass, "")
    host_port = host_rest.split("/", 1)[0]
    host = host_port.split(":")[0]
    port = int(host_port.split(":")[1]) if ":" in host_port else 3306
    import pymysql

    conn = pymysql.connect(host=host, port=port, user=user, password=pwd, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{_quote_db_name(db_name)}` CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()


def init_db():
    from admin import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)

    # 迁移：给 api_keys 表加 key_full 列（若不存在）
    _ensure_column("api_keys", "key_full", "VARCHAR(2048)", "DEFAULT ''")

    # 迁移：给 model_configs 表补齐倍率相关列（旧实例可能缺）
    _ensure_column("model_configs", "credit_multiplier", "FLOAT", "DEFAULT 0")
    _ensure_column("model_configs", "credits_raw", "VARCHAR(120)", "DEFAULT ''")

    # 迁移：给 schedules 表加 stop_after 列（daily_checkin 任务的「停止领取时间」）
    _ensure_column("schedules", "stop_after", "DATETIME", "NULL")

    # 迁移：给 usage_logs 表加详细用量与真实客户端 IP 列
    _ensure_column("usage_logs", "prompt_tokens", "INT", "NULL")
    _ensure_column("usage_logs", "completion_tokens", "INT", "NULL")
    _ensure_column("usage_logs", "total_tokens", "INT", "NULL")
    _ensure_column("usage_logs", "cached_tokens", "INT", "NULL")
    _ensure_column("usage_logs", "client_ip", "VARCHAR(64)", "DEFAULT ''")
    _ensure_column("usage_logs", "use_case", "VARCHAR(64)", "DEFAULT ''")
    # 迁移：给 usage_logs 表加请求级表格日志字段
    _ensure_column("usage_logs", "seq", "INT", "DEFAULT 0")
    _ensure_column("usage_logs", "ttfb_ms", "INT", "NULL")
    _ensure_column("usage_logs", "latency_ms", "INT", "NULL")
    _ensure_column("usage_logs", "error_kind", "VARCHAR(32)", "DEFAULT ''")

    # 迁移：给 accounts 表加稳定性状态机字段
    _ensure_column("accounts", "err_count", "INT", "DEFAULT 0")
    _ensure_column("accounts", "cool_until", "DATETIME", "NULL")
    _ensure_column("accounts", "cool_kind", "VARCHAR(16)", "DEFAULT ''")
    _ensure_column("accounts", "last_err_at", "DATETIME", "NULL")
    _ensure_column("accounts", "last_err_msg", "VARCHAR(255)", "DEFAULT ''")
    _ensure_column("accounts", "last_picked_at", "DATETIME", "NULL")
    # 迁移：给 accounts 表加成长中心连登天数（活跃展示；NULL=从未查过）
    _ensure_column("accounts", "streak_days", "INT", "NULL")

    # 迁移：创建 system_settings / schedules 表（create_all 已处理，这里仅兜底）


def _ensure_column(table: str, col: str, col_type: str, default: str = ""):
    """检查并添加缺失的列（MySQL 兼容）。表名/列名经白名单校验；其余参数走绑定。"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:tbl AND COLUMN_NAME=:col"
                ),
                {"tbl": table, "col": col},
            )
            exists = result.scalar() or 0
            if not exists:
                # 表名/列名为编译期常量，经白名单校验后安全内插；col_type/default 亦为常量
                conn.execute(
                    text(
                        f"ALTER TABLE `{_safe_ident(table)}` "
                        f"ADD COLUMN `{_safe_ident(col)}` {col_type} {default}"
                    )
                )
                conn.commit()
    except Exception:
        pass  # 非 MySQL 或权限不足时静默跳过
