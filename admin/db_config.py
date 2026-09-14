# -*- coding: utf-8 -*-
"""数据库配置中心 —— 全项目唯一的「数据库类型 + 连接参数」定义处。

取值优先级（高 → 低）：

  1. ADMIN_DATABASE_URL
     显式连接串，方言从 URL 里自动认——原有部署不动即可继续用：
        ADMIN_DATABASE_URL=mysql+pymysql://root:pwd@127.0.0.1:3306/workbuddy_admin?charset=utf8mb4
        ADMIN_DATABASE_URL=db2+ibm_db://db2inst1:pwd@127.0.0.1:50000/WBADMIN?currentSchema=WBADMIN
        ADMIN_DATABASE_URL=sqlite:///./data/workbuddy_admin.db

  2. ADMIN_DB_TYPE + 分项参数（推荐，切库只改一行）
        ADMIN_DB_TYPE=sqlite         # sqlite | mysql | db2
        ADMIN_DB_HOST=127.0.0.1
        ADMIN_DB_PORT=3306           # 留空则用方言默认端口（MySQL 3306 / DB2 50000）
        ADMIN_DB_USER=root
        ADMIN_DB_PASSWORD=xxx
        ADMIN_DB_NAME=workbuddy_admin
        ADMIN_DB_SCHEMA=             # DB2 专用（MySQL / SQLite 忽略）
        ADMIN_DB_OPTIONS=charset=utf8mb4

     SQLite 特殊：没有主机 / 端口 / 账号，文件名写在 ADMIN_DB_NAME 里即可
        ADMIN_DB_TYPE=sqlite
        ADMIN_DB_NAME=./data/workbuddy_admin.db     # 相对项目根目录；也可写绝对路径
        # 路径不存在时自动建目录（ADMIN_DB_AUTO_CREATE=1），文件不存在时自动建文件

  3. DEFAULT_DB_TYPE（当前 = sqlite）
     上面两项都没配时用它。SQLite 零依赖，clone 下来直接就能跑，适合本机开发；
     生产务必在 .env 里显式写 ADMIN_DB_TYPE，不要依赖这个兜底值。

其它开关：
        ADMIN_DB_AUTO_CREATE=1       # 自动建库(MySQL) / schema(DB2) / 数据文件目录(SQLite)
        ADMIN_DB_POOL_SIZE=20        ADMIN_DB_MAX_OVERFLOW=40
        ADMIN_DB_POOL_TIMEOUT=30     ADMIN_DB_POOL_RECYCLE=3600
        ADMIN_DB_CONNECT_TIMEOUT=10  ADMIN_DB_ECHO=0

自检：python -m admin.db_config   —— 打印当前生效的配置（密码已打码）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy.engine import make_url

from admin.db_dialect import DbDialect, dialect_from_url, get_dialect, supported_help

# 项目根目录：SQLite 的相对路径以它为基准（不受运行时 CWD 影响）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 什么都没配时用的默认数据库类型。
# 选 sqlite 是为了「开箱即跑」：零依赖、不用装数据库服务，克隆下来直接启动。
# 生产环境请在 .env 里显式写 ADMIN_DB_TYPE（mysql / db2），别依赖这个兜底值。
DEFAULT_DB_TYPE = "sqlite"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _load_dotenv() -> None:
    """独立于 admin/config.py 加载根目录 .env，保证本模块单独运行也生效。"""
    try:
        from dotenv import load_dotenv

        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(_root, ".env"))
    except Exception:
        pass


_load_dotenv()


def _env(name: str) -> Optional[str]:
    """读环境变量；未设置返回 None，设置为空串返回 ''。"""
    v = os.getenv(name)
    return v.strip() if isinstance(v, str) else None


def _flag(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    low = v.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ValueError(f"{name} 只能是布尔值（1/0/true/false），当前={v!r}")


def _int(name: str, default: int) -> int:
    v = _env(name)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{name} 必须是整数，当前={v!r}")


@dataclass(frozen=True)
class DbConfig:
    """解析后的数据库连接配置（不可变）。"""

    dialect: DbDialect
    url: str
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    name: str = ""
    schema: Optional[str] = None
    options: str = ""
    auto_create: bool = True
    connect_timeout: int = 10
    pool_size: int = 20
    max_overflow: int = 40
    pool_timeout: int = 30
    pool_recycle: int = 3600
    echo: bool = False

    @property
    def type(self) -> str:
        return self.dialect.key

    @property
    def label(self) -> str:
        return self.dialect.label

    @property
    def is_file_based(self) -> bool:
        """是否按「数据文件」连接（SQLite）：此时 host/port/user 无意义。"""
        return self.dialect.file_based

    def connect_args(self) -> dict:
        """传给 create_engine 的 connect_args（方言特有）。"""
        args: dict = {}
        if self.dialect.key == "mysql":
            args["connect_timeout"] = self.connect_timeout
        # ibm_db 的连接超时走 URL 参数 ConnectTimeout / connecttimeout，由 options 给出
        if self.dialect.key == "sqlite":
            # 默认 5 秒太短：定时任务 + 后台请求并发写入时容易直接抛
            # database is locked。给 30 秒让写锁有机会被等到。
            args["timeout"] = self.connect_timeout or 30
            # 线程池外的调度线程也会用同一连接池，必须允许跨线程
            args["check_same_thread"] = False
        return args

    def engine_kwargs(self) -> dict:
        """除 url / connect_args 之外传给 create_engine 的参数（方言特有）。

        SQLite 有两点特殊：
          - 它不支持 pool_size / max_overflow，用默认的 QueuePool 会直接报错，
            改用 NullPool：每个会话一条连接，配合 timeout 等待写锁，最稳；
          - pool_pre_ping / pool_recycle 对本地文件没有意义。
        """
        if self.dialect.file_based:
            from sqlalchemy.pool import NullPool

            return {"poolclass": NullPool}
        return {
            "pool_pre_ping": True,
            "pool_recycle": self.pool_recycle,
            "pool_timeout": self.pool_timeout,
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
        }

    def describe(self) -> str:
        """一行人类可读摘要（用于启动日志 / 自检）。"""
        if self.dialect.file_based:
            shape = f"{self.dialect.label} ({self.dialect.key}) @ {self.name}"
        else:
            port = self.port or self.dialect.default_port
            shape = (f"{self.dialect.label} ({self.dialect.key}) @ "
                     f"{self.host or '127.0.0.1'}:{port}/{self.name or self.dialect.default_database}")
        schema = f" schema={self.schema}" if self.schema else ""
        return f"{shape}{schema}"

    def safe_url(self) -> str:
        """隐藏密码后的 URL，可安全打日志。"""
        try:
            return make_url(self.url).render_as_string(hide_password=True)
        except Exception:
            return self.url.rsplit("@", 1)[-1]


def _normalize_sqlite_target(target: str) -> str:
    """把用户写的数据文件名规整成 SQLAlchemy 的 sqlite URL。

    SQLAlchemy 的写法是 `sqlite:///<绝对路径>`；想用相对路径必须写成
    `sqlite:///./x.db`（没有点的相对路径会被当成绝对路径）。
    这里把几种常见写法统一掉：
        :memory:                -> sqlite://
        sqlite:///./data/a.db   -> 原样
        D:/data/a.db            -> sqlite:///D:/data/a.db
        ./data/a.db / data/a.db -> sqlite:///./data/a.db
    """
    raw = (target or "").strip()
    if not raw:
        raw = "workbuddy_admin.db"
    if raw.lower() in (":memory:", "memory"):
        return "sqlite://"
    if raw.startswith("sqlite:"):
        return raw
    # Windows 绝对路径（D:\x / D:/x）与 POSIX 绝对路径（/x）都直接接在 ///
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", "\\\\")):
        path = raw.replace("\\", "/")
        return f"sqlite:///{path}"
    # 相对路径：补 ./，否则 SQLAlchemy 会当成绝对路径
    clean = raw[2:] if raw.startswith("./") else raw
    return f"sqlite:///./{clean.replace(chr(92), '/')}"


def _sqlite_file_path(url: str, project_root: str) -> Optional[str]:
    """从 sqlite URL 里取出真实文件路径（用于建目录）；内存库返回 None。"""
    u = make_url(url)
    db = u.database
    if not db:
        return None
    p = db.replace("/", os.sep)
    if not os.path.isabs(p):
        p = os.path.join(project_root, p.lstrip("./\\"))
    return os.path.normpath(p)


def _assemble_url(dia: DbDialect, host: str, port: int, user: str, password: str,
                  name: str, options: str) -> str:
    """把分项参数拼成 SQLAlchemy URL（用户 / 密码 / 库名均做 URL 转义，
    避免密码里的 @ / : / # 等特殊字符把连接串劈歪）。"""
    if dia.file_based:
        return _normalize_sqlite_target(name)
    auth = ""
    if user or password:
        auth = f"{quote_plus(user)}:{quote_plus(password or '')}@"
    return f"{dia.url_scheme}://{auth}{host}:{port}/{quote_plus(name)}" + (f"?{options}" if options else "")


def _build_url(dia: DbDialect, schema: Optional[str]) -> tuple:
    """返回 (url, host, port, user, password, name)。"""
    explicit = _env("ADMIN_DATABASE_URL")

    if explicit:
        u = make_url(explicit)
        if dia.file_based:
            # SQLite 的 database 就是文件路径，host / port / user 一律留空
            return (explicit, "", 0, "", "", u.database or dia.default_database)
        return (
            explicit,
            u.host or "",
            u.port or dia.default_port,
            u.username or "",
            u.password or "",
            u.database or "",
        )

    if dia.file_based:
        # 文件库：没有主机 / 端口 / 账号密码，ADMIN_DB_NAME 就是数据文件路径
        name = _env("ADMIN_DB_NAME") or dia.default_database
        url = _normalize_sqlite_target(name)
        return url, "", 0, "", "", name

    host = _env("ADMIN_DB_HOST") or "127.0.0.1"
    port = _int("ADMIN_DB_PORT", dia.default_port)
    user = _env("ADMIN_DB_USER")
    user = dia.default_user if user is None else user
    password = _env("ADMIN_DB_PASSWORD")
    password = dia.default_password if password is None else password
    name = _env("ADMIN_DB_NAME") or dia.default_database

    options = _env("ADMIN_DB_OPTIONS")
    if options is None:
        options = dia.default_options

    # DB2 的 schema 也能通过连接参数固化到会话上（比每次 SET CURRENT SCHEMA 更稳）
    kv = []
    if dia.url_schema_param and schema:
        kv.append(f"{dia.url_schema_param}={quote_plus(schema)}")
    if options:
        kv.append(options.lstrip("?&"))
    merged = "&".join(kv)

    return _assemble_url(dia, host, port, user, password, name, merged), host, port, user, password, name


def resolve() -> DbConfig:
    """读取环境变量，产出一份不可变的 DbConfig。"""
    type_raw = _env("ADMIN_DB_TYPE")

    # 方言的判定：显式 URL 优先从 URL 识别；否则以 ADMIN_DB_TYPE 为准
    # 两者都没给时用 DEFAULT_DB_TYPE —— 默认 SQLite（零依赖，开箱即跑），
    # 生产请在 .env 里显式写上 ADMIN_DB_TYPE。
    explicit = _env("ADMIN_DATABASE_URL")
    if explicit and not type_raw:
        dia = dialect_from_url(explicit)
    else:
        dia = get_dialect(type_raw or DEFAULT_DB_TYPE)

    schema = _env("ADMIN_DB_SCHEMA") or None
    if not schema and explicit and dia.url_schema_param:
        schema = make_url(explicit).query.get(dia.url_schema_param)

    url, host, port, user, password, name = _build_url(dia, schema)

    return DbConfig(
        dialect=dia,
        url=url,
        host=host,
        port=port,
        user=user,
        password=password,
        name=name,
        schema=schema,
        options=("" if explicit else (_env("ADMIN_DB_OPTIONS") or dia.default_options or "")),
        auto_create=_flag("ADMIN_DB_AUTO_CREATE", True),
        connect_timeout=_int("ADMIN_DB_CONNECT_TIMEOUT", 10),
        pool_size=_int("ADMIN_DB_POOL_SIZE", 20),
        max_overflow=_int("ADMIN_DB_MAX_OVERFLOW", 40),
        pool_timeout=_int("ADMIN_DB_POOL_TIMEOUT", 30),
        pool_recycle=_int("ADMIN_DB_POOL_RECYCLE", 3600),
        echo=_flag("ADMIN_DB_ECHO", False),
    )


db_config: DbConfig = resolve()

# 兼容旧写法：admin/config.py 仍通过 DATABASE_URL 暴露最终连接串
DATABASE_URL: str = db_config.url


def _main() -> None:
    """python -m admin.db_config：打印当前生效的数据库配置（密码打码）。"""
    c = db_config
    print("数据库配置（来自环境变量 / .env）")
    print("-" * 56)
    print(f"类型      : {c.type}  ({c.label})")
    print(f"连接串    : {c.safe_url()}")
    if c.is_file_based:
        path = _sqlite_file_path(c.url, PROJECT_ROOT)
        print(f"数据文件  : {path or '(内存库)'}")
        print(f"文件存在  : {os.path.isfile(path) if path else '-'}")
        print("连接池    : NullPool（文件库；写锁超时 "
              f"{c.connect_args().get('timeout')}s）")
        print("说明      : SQLite 是单写者模型，不适合高并发写入场景")
    else:
        print(f"主机/端口 : {c.host or '127.0.0.1'}:{c.port or c.dialect.default_port}")
        print(f"账号      : {c.user}")
        print(f"库名      : {c.name or c.dialect.default_database}")
        print(f"Schema    : {c.schema or '(默认)'}")
        print(f"连接池    : size={c.pool_size} overflow={c.max_overflow} "
              f"timeout={c.pool_timeout}s recycle={c.pool_recycle}s")
    print(f"自动建库  : {c.auto_create}")
    print("-" * 56)
    print(f"可选类型  : {supported_help()}")


if __name__ == "__main__":
    _main()
