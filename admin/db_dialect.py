# -*- coding: utf-8 -*-
"""数据库方言适配层（MySQL / DB2 / SQLite）。

整个项目里真正依赖具体数据库的只有这几件事：
  - 连接串的 scheme（mysql+pymysql / db2+ibm_db / sqlite）与默认端口；
  - 「某一列存不存在」的系统表查询写法；
  - ALTER TABLE ADD COLUMN 的 DDL 与类型名；
  - 启动前是否需要先准备「库」（MySQL）/「schema」（DB2）/「数据文件」（SQLite）。

其余增删改查全部走 ORM，由 SQLAlchemy 自己处理方言差异，本模块一概不碰。

新增一种数据库：在下面 _DIALECTS 里注册一条 DbDialect 即可，业务代码无需改动。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from sqlalchemy.engine import make_url

# SQL 标识符白名单：仅允许常规字母数字下划线，杜绝任何拼接注入。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_ident(name: str) -> str:
    """校验并原样返回标识符；含引号 / 空格 / 分号一律拒绝。"""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"非法 SQL 标识符: {name!r}")
    return name


@dataclass(frozen=True)
class DbDialect:
    """一种数据库方言的差异描述。"""

    key: str                    # 配置里写的类型名：mysql / db2 / sqlite
    label: str                  # 人类可读名称（日志 / 报错里显示）
    url_scheme: str             # SQLAlchemy URL 的 scheme
    driver_packages: tuple      # 依赖自检用的 import / pip 包名
    default_port: int
    default_user: str
    default_password: str
    default_database: str
    default_options: str        # 拼接 URL 时默认带的方言参数串
    quote: str                  # 标识符引用符：MySQL 用反引号，DB2 用双引号
    supports_create_database: bool
    supports_create_schema: bool
    url_schema_param: str       # URL 里指定默认 schema 的参数名（无此能力则空串）
    no_default_types: tuple     # 这些 DDL 类型不允许带 DEFAULT（DB2 的 LOB 就是这种）
    type_map: Dict[str, str]    # 通用类型 -> DDL 类型模板（{length} 占位）
    column_exists_sql: str      # 查「列是否存在」，用 :tbl / :col 绑定参数
    current_schema_expr: str    # SQL 中取「当前 schema / 库名」的表达式
    # 系统表里标识符的大小写。MySQL 不区分（原样比较）；DB2 存大写；
    # SQLite 的 sqlite_master 存「建表时怎么写就怎么存」，即小写。
    name_case: str = "asis"     # asis | upper | lower
    # 是否按「数据文件」连接（SQLite）：无主机 / 端口 / 账号概念
    file_based: bool = False
    # 标识符引用符的额外别名（便于生成可读的报错提示）
    extra_aliases: Tuple[str, ...] = field(default_factory=tuple)

    # ---------- 标识符 ----------
    def q(self, name: str) -> str:
        """安全引用标识符（已过白名单，可直接拼接进 DDL）。"""
        return f"{self.quote}{validate_ident(name)}{self.quote}"

    def upper_name(self, name: str) -> str:
        """按方言规范系统表里标识符的大小写（查 SQLite 必须用小写表名）。"""
        if self.name_case == "upper":
            return name.upper()
        if self.name_case == "lower":
            return name.lower()
        return name

    # ---------- 类型 ----------
    def ddl_type(self, generic: str, length: Optional[int] = None) -> str:
        """把通用类型名（str/int/float/datetime/bool/text）翻译成方言 DDL 类型。"""
        tpl = self.type_map.get(generic)
        if not tpl:
            raise ValueError(f"方言 {self.key} 不支持的通用类型: {generic}")
        return tpl.format(length=int(length) if length else 128)

    # ---------- DDL ----------
    def allows_default(self, ddl_type: str) -> bool:
        """该 DDL 类型能否带 DEFAULT 子句（DB2 的 LOB 类型不允许）。"""
        head = (ddl_type or "").strip().upper()
        return not self.no_default_types or not head.startswith(tuple(self.no_default_types))

    def alter_add_column(self, table: str, column: str, ddl_type: str,
                         default: Optional[str] = None) -> str:
        """生成「追加一列」的 DDL。

        default 传 Python 侧的字面量片段（如 "''" / "0"），传 None 表示可空且不带 DEFAULT。
        类型本身不支持默认值时（如 DB2 的 CLOB）自动省略 DEFAULT 子句。
        """
        parts = ["ALTER TABLE", self.q(table), "ADD COLUMN", self.q(column), ddl_type]
        if default is not None and self.allows_default(ddl_type):
            parts += ["DEFAULT", default]
        return " ".join(parts)

    def create_schema_ddl(self, schema: str) -> str:
        return f"CREATE SCHEMA {self.q(schema)}"

    def install_driver_hint(self) -> str:
        return "pip install " + " ".join(self.driver_packages)


MYSQL = DbDialect(
    key="mysql",
    label="MySQL / MariaDB",
    url_scheme="mysql+pymysql",
    driver_packages=("pymysql",),
    default_port=3306,
    default_user="root",
    default_password="root",
    default_database="workbuddy_admin",
    default_options="charset=utf8mb4",
    quote="`",
    supports_create_database=True,
    supports_create_schema=False,
    url_schema_param="",
    no_default_types=(),
    type_map={
        "str": "VARCHAR({length})",
        "int": "INT",
        "float": "FLOAT",
        "datetime": "DATETIME",
        "bool": "TINYINT(1)",
        "text": "TEXT",
    },
    column_exists_sql=(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:tbl AND COLUMN_NAME=:col"
    ),
    current_schema_expr="DATABASE()",
)

DB2 = DbDialect(
    key="db2",
    label="IBM Db2 (LUW)",
    url_scheme="db2+ibm_db",
    driver_packages=("ibm_db_sa",),
    default_port=50000,
    default_user="db2inst1",
    default_password="",
    default_database="WBADMIN",
    default_options="",
    quote='"',
    supports_create_database=False,   # DB2 建库是实例级操作，不在应用里做
    supports_create_schema=True,
    url_schema_param="currentSchema",
    # DB2 的 LOB 列不允许指定非 NULL 默认值，补列时必须省略 DEFAULT 子句
    no_default_types=("CLOB", "BLOB", "NCLOB", "DBCLOB"),
    type_map={
        # DB2 用 TIMESTAMP 对应 MySQL 的 DATETIME；TEXT 对应 CLOB。
        "str": "VARCHAR({length})",
        "int": "INTEGER",
        "float": "DOUBLE",
        "datetime": "TIMESTAMP",
        "bool": "SMALLINT",
        "text": "CLOB",
    },
    column_exists_sql=(
        "SELECT COUNT(*) FROM SYSCAT.COLUMNS "
        "WHERE TABSCHEMA = CURRENT SCHEMA "
        "AND TABNAME = :tbl AND COLNAME = :col"
    ),
    current_schema_expr="CURRENT SCHEMA",
    name_case="upper",
)

# SQLite：单文件库，零依赖（驱动 sqlite3 是 Python 标准库），适合单机 / 试跑 / 小规模部署。
# 注意：SQLite 是「单写者」模型，高并发写入会报 database is locked，
# 且 SQLAlchemy 的 QueuePool 参数对它不适用，连接参数在 admin/db_config.py 里单独处理。
SQLITE = DbDialect(
    key="sqlite",
    label="SQLite（单文件）",
    url_scheme="sqlite",
    driver_packages=(),          # sqlite3 是标准库，无需安装
    default_port=0,
    default_user="",
    default_password="",
    default_database="workbuddy_admin.db",
    default_options="",
    quote='"',
    supports_create_database=False,   # 数据文件由 SQLAlchemy 自动创建，无需建库语句
    supports_create_schema=False,
    url_schema_param="",
    no_default_types=(),
    type_map={
        # SQLite 用动态类型，声明类型只为可读；INTEGER PRIMARY KEY 才能自增（ORM 已处理）。
        "str": "VARCHAR({length})",
        "int": "INTEGER",
        "float": "FLOAT",
        "datetime": "TIMESTAMP",
        "bool": "SMALLINT",
        "text": "TEXT",
    },
    column_exists_sql=(
        "SELECT COUNT(*) FROM PRAGMA_TABLE_INFO(:tbl) WHERE name = :col"
    ),
    current_schema_expr="",
    name_case="asis",            # sqlite_master / PRAGMA_TABLE_INFO 用小写表名
    file_based=True,
)

_DIALECTS: Dict[str, DbDialect] = {"mysql": MYSQL, "db2": DB2, "sqlite": SQLITE}

# 常见别名，写错也能落到正确方言
_ALIASES: Dict[str, str] = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "db2": "db2",
    "db2luw": "db2",
    "ibmdb2": "db2",
    "ibm_db": "db2",
    "ibm_db_sa": "db2",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "file": "sqlite",
}


def supported_keys() -> list:
    """返回所有可用方言的规范 key。"""
    return sorted(_DIALECTS)


def supported_help() -> str:
    return " / ".join(f"{k}({_DIALECTS[k].label})" for k in supported_keys())


def get_dialect(key: str) -> DbDialect:
    """按 key 或别名取方言；不支持则抛出带提示的 ValueError。"""
    norm = (key or "").strip().lower()
    real = _ALIASES.get(norm)
    if not real:
        raise ValueError(f"不支持的数据库类型 {key!r}，可选：{supported_help()}")
    return _DIALECTS[real]


def dialect_from_url(url: str) -> DbDialect:
    """从 SQLAlchemy URL 里认方言：mysql+pymysql / db2+ibm_db / ibm_db_sa ..."""
    drivername = make_url(url).drivername
    base = drivername.split("+", 1)[0]
    try:
        return get_dialect(base)
    except ValueError:
        return get_dialect(drivername)


def ddl_type(dialect: DbDialect, generic: str, length: Optional[int] = None) -> str:
    """便捷函数：拿 DDL 类型串（迁移加列时用）。"""
    return dialect.ddl_type(generic, length)
