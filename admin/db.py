# -*- coding: utf-8 -*-
"""SQLAlchemy 引擎 / 会话 / Base，并负责建库（DB2 是建 schema）与建表。

数据库类型与连接参数由 admin/db_config.py 统一解析（env / .env），
方言差异（引用符、系统表、DDL 类型、能否自动建库）由 admin/db_dialect.py 抹平，
除「自动建库还是建 schema」这种方言能力差异外，本模块不再针对具体库写分支；
加一种数据库主要就是在那两个文件里登记。
"""
import os
import re

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import NoSuchModuleError
from sqlalchemy.orm import declarative_base, sessionmaker

from admin.config import settings
from admin.db_config import PROJECT_ROOT, _sqlite_file_path, db_config
from admin.db_dialect import DbDialect, ddl_type as _ddl_type

# SQL 标识符白名单：仅允许常规字母数字下划线，杜绝任何拼接注入。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DIALECT: DbDialect = db_config.dialect


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"非法 SQL 标识符: {name!r}")
    return name


def _quote_db_name(name: str) -> str:
    """库名 / schema 名（来自配置）去掉引号与转义后安全引用。"""
    clean = (name or "").replace("`", "").replace('"', "").replace("\\", "").strip()
    return DIALECT.q(clean)


_engine_kwargs = {"future": True, "echo": db_config.echo}
# 连接池参数按方言给（SQLite 用 NullPool，见 DbConfig.engine_kwargs）
_engine_kwargs.update(db_config.engine_kwargs())
# connect_args 只允许 dict，空字典也不必传（None 会让 SQLAlchemy 报错）
_connect_args = db_config.connect_args()
if _connect_args:
    _engine_kwargs["connect_args"] = _connect_args

try:
    engine = create_engine(db_config.url, **_engine_kwargs)
except (ModuleNotFoundError, NoSuchModuleError) as e:  # 选了某种数据库却没装对应驱动
    raise type(e)(
        f"当前数据库类型 ADMIN_DB_TYPE={db_config.type} 缺少驱动，请先执行："
        f"{DIALECT.install_driver_hint()}    （原始错误：{e}）"
    ) from e
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


# DB2 的每个连接都要保证 current schema 正确（连接池复用不会保留上一条 SQL 设置的会话状态）。
if DIALECT.supports_create_schema and db_config.schema:
    _SCHEMA_DDL = f"SET CURRENT SCHEMA {DIALECT.q(db_config.schema)}"

    @event.listens_for(engine, "connect")
    def _set_db2_schema(dbapi_conn, _record):  # noqa: ANN001  SQLAlchemy 事件签名固定
        cur = dbapi_conn.cursor()
        try:
            cur.execute(_SCHEMA_DDL)
        except Exception:
            pass  # 连接级失败由后续查询报错暴露，这里不能把连接池搞崩
        finally:
            try:
                cur.close()
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 建库 / 建 schema
# ---------------------------------------------------------------------------
def _mysql_create_database():
    """MySQL：库不存在则创建（用 make_url 解析，密码里的特殊字符不会错位）。"""
    import pymysql

    conn = pymysql.connect(host=db_config.host, port=db_config.port,
                           user=db_config.user, password=db_config.password,
                           charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_quote_db_name(db_config.name)} "
                        f"CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()


def _db2_create_schema():
    """DB2：不能由应用建库（实例级操作），改为保证目标 schema 存在。"""
    schema = db_config.schema
    if not schema:
        return
    with engine.connect() as conn:
        try:
            conn.execute(text(DIALECT.create_schema_ddl(schema)))
            conn.commit()
        except Exception:
            conn.rollback()  # 已存在（SQLSTATE 42710）或权限不足时忽略


def _sqlite_prepare_file():
    """SQLite：建好数据文件所在目录（文件本身由 SQLAlchemy 首次连接时创建）。"""
    path = _sqlite_file_path(db_config.url, PROJECT_ROOT)
    if not path or path == ":memory:":
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def ensure_database():
    """按方言保证「库 / schema / 数据文件目录」存在。失败不阻断启动，由后续 connect 报真正的错。"""
    if not db_config.auto_create:
        return
    try:
        if DIALECT.key == "mysql":
            _mysql_create_database()
        elif DIALECT.file_based:
            _sqlite_prepare_file()
        elif DIALECT.supports_create_schema:
            _db2_create_schema()
    except Exception as e:
        # 连接不上 / 账号没权限等情况统一降级：给提示，让启动继续，
        # 真正的失败会在 init_db() 或首个请求时以清晰报错暴露。
        print(f"[db] 跳过自动建库/建 schema（{e}）")


# ---------------------------------------------------------------------------
# 列迁移
# ---------------------------------------------------------------------------
def _resolve_col_type(table: str, col: str, generic_type: str,
                      length: int | None = None) -> str:
    """优先从 ORM 模型取该列的类型，让「建表」与「补列」用同一套类型。

    模型里给 Text / 超长 String 配了方言变体（MySQL 用 TEXT/VARCHAR，DB2 用 CLOB），
    走 ORM 才能保证增量迁移和 create_all 完全一致；取不到时退回通用类型映射。
    """
    meta_col = None
    try:
        tbl = Base.metadata.tables.get(table)
        if tbl is not None and col in tbl.columns:
            meta_col = tbl.columns[col].type
    except Exception:
        meta_col = None
    if meta_col is not None:
        try:
            compiler = getattr(engine.dialect, "type_compiler_instance", None) \
                or engine.dialect.type_compiler
            return compiler.process(meta_col)
        except Exception:
            pass
    return _ddl_type(DIALECT, generic_type, length)


def _ensure_column(table: str, col: str, generic_type: str,
                   default: str | None = None, length: int | None = None):
    """若列不存在则追加一列（跨方言）。

    :param generic_type: 通用类型名 str/int/float/datetime/bool/text（ORM 取不到类型时的兜底）
    :param default:      字面量片段，如 "''" / "0"；None 表示可空且不带 DEFAULT
    :param length:       str 的长度；缺省 128
    表名 / 列名经白名单校验后拼接，其余值走绑定参数或受控字面量。
    """
    tbl, name = _safe_ident(table), _safe_ident(col)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text(DIALECT.column_exists_sql),
                {"tbl": DIALECT.upper_name(tbl), "col": DIALECT.upper_name(name)},
            ).scalar() or 0
            if exists:
                return
            col_type = _resolve_col_type(tbl, name, generic_type, length)
            conn.execute(text(DIALECT.alter_add_column(tbl, name, col_type, default)))
            conn.commit()
    except Exception:
        pass  # 方言不支持该查询 / 权限不足时静默跳过，不影响主流程


def init_db():
    from admin import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)

    # 迁移：给 api_keys 表加 key_full 列（若不存在）
    _ensure_column("api_keys", "key_full", "str", "''", 2048)

    # 迁移：给 model_configs 表补齐倍率相关列（旧实例可能缺）
    _ensure_column("model_configs", "credit_multiplier", "float", "0")
    _ensure_column("model_configs", "credits_raw", "str", "''", 120)

    # 迁移：给 schedules 表加 stop_after 列（daily_checkin 任务的「停止领取时间」）
    _ensure_column("schedules", "stop_after", "datetime")

    # 迁移：给 usage_logs 表加详细用量与真实客户端 IP 列
    _ensure_column("usage_logs", "prompt_tokens", "int")
    _ensure_column("usage_logs", "completion_tokens", "int")
    _ensure_column("usage_logs", "total_tokens", "int")
    _ensure_column("usage_logs", "cached_tokens", "int")
    _ensure_column("usage_logs", "client_ip", "str", "''", 64)
    _ensure_column("usage_logs", "use_case", "str", "''", 64)
    # 迁移：给 usage_logs 表加请求级表格日志字段
    _ensure_column("usage_logs", "seq", "int", "0")
    _ensure_column("usage_logs", "ttfb_ms", "int")
    _ensure_column("usage_logs", "latency_ms", "int")
    _ensure_column("usage_logs", "error_kind", "str", "''", 32)

    # 迁移：给 accounts 表加稳定性状态机字段
    _ensure_column("accounts", "err_count", "int", "0")
    _ensure_column("accounts", "cool_until", "datetime")
    _ensure_column("accounts", "cool_kind", "str", "''", 16)
    _ensure_column("accounts", "last_err_at", "datetime")
    _ensure_column("accounts", "last_err_msg", "str", "''", 255)
    _ensure_column("accounts", "last_picked_at", "datetime")
    # 迁移：给 accounts 表加成长中心连登天数（活跃展示；NULL=从未查过）
    _ensure_column("accounts", "streak_days", "int")

    # 迁移：创建 system_settings / schedules 表（create_all 已处理，这里仅兜底）
