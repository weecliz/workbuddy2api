#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 一键拉起 workbuddy2api（单端口单进程部署）。

本项目对外只暴露「一个端口」即可：
  - 管理后台前端（index.html）与后端 API：http://127.0.0.1:8790/admin
  - 对外共享的托管网关（带 Key 校验 / 配额 / 用量记账 / 号池调度）：
        http://127.0.0.1:8790/v1/chat/completions   （OpenAI 协议）
        http://127.0.0.1:8790/v1/responses          （Responses 协议）
        http://127.0.0.1:8790/v1/messages           （Anthropic 协议，Claude Code 用）
        http://127.0.0.1:8790/v1/models
  - 内嵌的独立网关 converter（本机桌面登录态直连，无 Key 配额；额外支持 /v1/balance）：
        http://127.0.0.1:8790/gw/v1/...

converter 已在 admin/server.py 中挂载到 /gw 前缀，因此无需再单独开 8787 端口/进程。
（若你确实需要独立运行 converter 在 8787，直接 `python converter.py --desensitize` 即可，与本脚本互不冲突。）

子进程输出实时 tee 到控制台 + logs/ 日志；Ctrl+C / 关闭终端优雅关闭。

用法:
  python main.py                  # 前台常驻，监听 0.0.0.0:8790
                                  # 数据库：默认 SQLite（零依赖，开箱即跑）
  python main.py --port 8790
  python main.py --host 127.0.0.1
  python main.py --db-type mysql --db-password xxx   # 本次启动临时改用 MySQL
  python main.py --db-type sqlite --db-name ./data/wb.db

命令行参数（优先级高于 .env，只影响本次进程，不回写配置文件）:
  --host / --port      : 监听地址 / 端口
  --db-type            : 数据库类型 sqlite | mysql | db2（等价于临时覆盖 ADMIN_DB_TYPE）
  --db-host / --db-port / --db-user / --db-password
  --db-name            : 库名；SQLite 时是数据文件路径
  --db-schema          : DB2 目标 schema
  --db-options         : 方言原生参数串，如 charset=utf8mb4
  --list-db-types      : 只列出支持的数据库类型后退出

环境变量（可选，覆盖默认；部署务必设置）:
  ADMIN_PORT / ADMIN_HOST
  ADMIN_DB_TYPE / ADMIN_DB_HOST / ADMIN_DB_PORT / ADMIN_DB_USER /
  ADMIN_DB_PASSWORD / ADMIN_DB_NAME / ADMIN_DB_SCHEMA / ADMIN_DB_OPTIONS
  ADMIN_JWT_SECRET  : 生产务必覆盖为 >=32 字节随机串（默认是弱密钥，会报警）
  ADMIN_USERNAME / ADMIN_PASSWORD     : 后台登录凭据（默认 admin / admin123）
  CONVERTER_DESENSITIZE / CONVERTER_LOG : 内嵌网关的脱敏开关与日志路径
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable  # 用运行本脚本的同一个解释器（venv / 系统都可）
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

_PRINT_LOCK = threading.Lock()


def _console_write(msg: str) -> None:
    """往控制台写一行，并容忍终端编码不支持某些字符。

    Windows 控制台默认是 GBK，本项目日志里有 ⚠ / ❌ / ✅ 这类符号，
    直接 print 会抛 UnicodeEncodeError 把启动整个打断 —— 必须降级成可用字符
    或忽略，绝不能因为「日志打不出来」而启动失败。
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = (getattr(sys.stdout, "encoding", None) or "ascii")
        safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
        try:
            print(safe, flush=True)
        except Exception:
            try:
                print(msg.encode("ascii", errors="ignore").decode("ascii"), flush=True)
            except Exception:
                pass  # 实在打不出来也不能让启动崩


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        _console_write(msg)


def _pump(stream, log_path: Path, tag: str) -> None:
    """把子进程输出实时打到控制台并写入日志文件（类 tee，文本模式）。"""
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            for raw in iter(stream.readline, ""):
                if not raw:
                    break
                text = raw.rstrip("\n")
                _log(f"[{tag}] {text}")
                lf.write(raw)
                lf.flush()
    except Exception:
        pass


def _display_host(host: str) -> str:
    """把「监听地址」换算成「浏览器能直接打开的地址」。

    0.0.0.0 / :: / * 是绑定用的通配地址，只表示「监听所有网卡」，
    并不是一个可访问的主机名 —— 直接印进 URL 用户点不开，所以这里统一
    换算成本机回环地址；其余地址原样返回。
    """
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "*", "::", "[::]", "0:0:0:0:0:0:0:0"):
        return "127.0.0.1"
    if ":" in h and not h.startswith("["):
        return f"[{h}]"  # IPv6 字面量放进 URL 需要方括号
    return h


def _lan_ip() -> str | None:
    """尽力探测本机在局域网中的 IPv4 地址；拿不到返回 None。

    用 UDP connect 探测默认出口网卡 —— 只是设置对端地址、不会真的发包，
    因此断网时也能拿到本机地址（只要有默认路由）。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _db_driver_deps() -> list:
    """按当前数据库类型返回该方言需要的驱动包。

    数据库类型来自 --db-type / ADMIN_DB_TYPE（见 .env），默认见 DEFAULT_DB_TYPE（sqlite）。
    sqlalchemy 本身还没装也不会在这里崩，交给下面的依赖自检统一给出安装提示。
    SQLite 不需要驱动（sqlite3 是标准库），返回空列表即可。
    """
    try:
        from admin.db_dialect import get_dialect
        return list(get_dialect(_db_type_from_env_or_argv()).driver_packages)
    except Exception:
        return []  # 读不到配置时按默认方言（sqlite）处理：它不需要任何驱动


def _db_type_from_env_or_argv() -> str:
    """取数据库类型：命令行 --db-type 优先，其次 .env / 环境变量，最后 DEFAULT_DB_TYPE。

    先看 sys.argv 是因为依赖自检发生在 argparse 之后没有意义的地方 ——
    必须在切换解释器之前就知道要检查哪个驱动（比如选 sqlite 就不该强求 pymysql）。
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--db-type" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--db-type="):
            return a.split("=", 1)[1]
    if os.getenv("ADMIN_DB_TYPE"):
        return os.getenv("ADMIN_DB_TYPE")
    # 都没给时与下面的 _apply_db_args 保持一致：显式写死 sqlite 兜底，
    # 因为这时候连 admin.db_config 都不一定能导入成功。
    return "sqlite"


# 命令行参数 -> 环境变量名。仅覆盖本次进程，不回写 .env。
_DB_CLI_ENV_MAP = {
    "db_type": "ADMIN_DB_TYPE",
    "db_host": "ADMIN_DB_HOST",
    "db_port": "ADMIN_DB_PORT",
    "db_user": "ADMIN_DB_USER",
    "db_password": "ADMIN_DB_PASSWORD",
    "db_name": "ADMIN_DB_NAME",
    "db_schema": "ADMIN_DB_SCHEMA",
    "db_options": "ADMIN_DB_OPTIONS",
}


def _apply_db_args(args) -> None:
    """把命令行里显式给出的数据库参数写进 os.environ（覆盖 .env 的值）。

    必须在任何 admin.* 模块被导入之前调用：admin.config / admin.db_config 在
    模块导入时就会解析环境变量并据此建引擎，晚一步就来不及了。
    """
    applied = []
    for attr, env_name in _DB_CLI_ENV_MAP.items():
        val = getattr(args, attr, None)
        if val is not None and str(val) != "":
            os.environ[env_name] = str(val)
            applied.append(f"{env_name}={val}")
            # 显式给了分项参数，就不该再让旧的整串 URL 把配置顶掉
            if attr == "db_type":
                os.environ.pop("ADMIN_DATABASE_URL", None)
    if applied:
        _log("[main] 命令行数据库参数：" + "  ".join(applied))


# 运行后台所需的核心依赖；当前解释器缺任一即尝试切换到带依赖的虚拟环境。
_REQUIRED_DEPS = ["uvicorn", "fastapi", "redis", "sqlalchemy"] + _db_driver_deps()


def _interpreter_with_deps() -> str | None:
    """返回带全部依赖的 python 解释器路径。

    优先用当前解释器；若缺包，则回退到项目内 .venv/venv 或本机 managed venv。
    都找不到返回 None（调用方给出安装提示后退出）。
    """
    try:
        import importlib
        for m in _REQUIRED_DEPS:
            importlib.import_module(m)
        return sys.executable
    except Exception:
        pass
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "bin" / "python",
        Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    ]
    for c in candidates:
        if not c.exists():
            continue
        try:
            subprocess.run(
                [str(c), "-c", "import " + ", ".join(_REQUIRED_DEPS)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return str(c)
        except Exception:
            continue
    return None


def _resolve_port(cli_port) -> int:
    """端口优先级：显式 `--port` > 环境变量 `ADMIN_PORT` > 8790。

    显式传参必须压过 .env。否则 `python main.py --port 8791` 会去绑 .env 里的
    8790：线上服务在跑时它直接 bind 失败退出；线上服务**不在跑**时它会占住
    8790 —— 把「起个测试实例」变成「打死线上服务」。
    """
    raw = cli_port if cli_port is not None else os.getenv("ADMIN_PORT", "8790")
    return int(raw)


def _resolve_host(cli_host) -> str:
    """监听地址优先级：显式 `--host` > 环境变量 `ADMIN_HOST` > 0.0.0.0。"""
    if cli_host:
        return str(cli_host)
    return os.getenv("ADMIN_HOST", "0.0.0.0")


def _build_admin_cmd(args) -> tuple[list[str], int, str]:
    port = _resolve_port(args.port)
    host = _resolve_host(args.host)
    cmd = [PY, "-m", "uvicorn", "admin.server:app",
           "--host", host, "--port", str(port), "--log-level", "info"]
    return cmd, port, host


def _build_parser() -> argparse.ArgumentParser:
    """统一的命令行定义（--db-type 等数据库参数在启动阶段就要生效）。"""
    ap = argparse.ArgumentParser(
        description="workbuddy2api 一键启动（单端口：管理后台 + 内嵌网关）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="数据库类型可选 mysql / db2 / sqlite；命令行参数优先级高于 .env，只影响本次启动。\n"
               "例：python main.py --db-type sqlite --db-name ./data/wb.db",
    )
    ap.add_argument("--host", default=None,
                    help="监听地址（显式指定时压过 ADMIN_HOST，缺省取 ADMIN_HOST / 0.0.0.0）")
    ap.add_argument("--port", type=int, default=None,
                    help="服务端口（显式指定时压过 ADMIN_PORT，缺省取 ADMIN_PORT / 8790）")

    g = ap.add_argument_group("数据库（覆盖 .env 里的同名配置，仅本次启动有效）")
    g.add_argument("--db-type", help="数据库类型：mysql | db2 | sqlite")
    g.add_argument("--db-host", help="数据库主机（SQLite 忽略）")
    g.add_argument("--db-port", help="数据库端口（留空用该类型默认值）")
    g.add_argument("--db-user", help="数据库账号（SQLite 忽略）")
    g.add_argument("--db-password", help="数据库密码（SQLite 忽略）")
    g.add_argument("--db-name", help="库名；SQLite 时为数据文件路径")
    g.add_argument("--db-schema", help="DB2 目标 schema（MySQL / SQLite 忽略）")
    g.add_argument("--db-options", help="方言原生参数串，如 charset=utf8mb4")
    ap.add_argument("--list-db-types", action="store_true",
                    help="列出支持的数据库类型后退出")
    return ap


def _print_db_types() -> None:
    """打印支持的数据库类型及其默认连接参数。"""
    from admin.db_dialect import get_dialect, supported_keys

    print("支持的数据库类型（--db-type）")
    print("-" * 64)
    for k in supported_keys():
        d = get_dialect(k)
        driver = "、".join(d.driver_packages) or "无需安装（标准库）"
        if d.file_based:
            target = f"数据文件 {d.default_database}"
        else:
            target = f"{d.default_host if hasattr(d, 'default_host') else '127.0.0.1'}:{d.default_port}"
        print(f"  {k:<8} {d.label:<18} 默认 {target:<26} 驱动 {driver}")
    print("-" * 64)
    print("例：python main.py --db-type sqlite --db-name ./data/wb.db")
    print("    python main.py --db-type db2 --db-name WBADMIN --db-schema WBADMIN")


def main() -> None:
    args = _build_parser().parse_args()

    if args.list_db_types:
        _print_db_types()
        return

    # 数据库参数必须在这里就落地：下面切换解释器时会通过 os.environ 透传，
    # 且 admin.db_config / admin.db 在导入时就会读取它们。
    _apply_db_args(args)

    # 依赖自检：当前解释器缺包则自动切换到带依赖的虚拟环境
    # （修复系统 Python 缺 pymysql 导致启动即崩退出）。注意 _REQUIRED_DEPS 是在
    # 模块导入时按数据库类型算好的，所以 --db-type 必须在导入前就可用 ——
    # 这也是上面先解析参数、_db_driver_deps 去读 sys.argv 的原因。
    py = _interpreter_with_deps()
    if py is None:
        _log("❌ 当前 Python 缺少依赖：" + ", ".join(_REQUIRED_DEPS))
        _log("   请先安装：pip install -r requirements.txt，或激活已装好依赖的虚拟环境后再运行。")
        try:
            from admin.db_config import db_config
            _log(f"   当前数据库类型 {db_config.type}（{db_config.label}），专用驱动："
                 f"{db_config.dialect.install_driver_hint()}")
        except Exception:
            pass
        sys.exit(2)
    if py != sys.executable:
        _log(f"[main] 当前解释器缺依赖，自动改用虚拟环境：{py}")
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])

    # 加载项目根目录的 .env，与 admin/config.py 保持一致。
    # 不加载的话，下面基于 os.getenv 的安全告警看不到 .env 里配置的值，会误报
    # 「未设置强 ADMIN_JWT_SECRET」；子进程（uvicorn）自己会加载 .env，
    # 于是出现「实际用了强密钥、却仍告警」的矛盾现象。
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass  # 没装 python-dotenv 时静默跳过（不影响启动）

    # 回显本次生效的数据库（类型 + 目标），数据库类型选错时一眼能看出来
    try:
        from admin.db_config import db_config
        _log(f"[main] 数据库：{db_config.describe()}")
    except Exception as e:
        _log(f"⚠️  数据库配置有问题：{e}")

    # 部署安全检查
    secret = os.getenv("ADMIN_JWT_SECRET", "")
    if not secret or secret.startswith("workbuddy-admin-jwt-secret-please-change"):
        _log("⚠️  未设置强 ADMIN_JWT_SECRET，将使用默认弱密钥 —— 部署请务必通过环境变量覆盖！")
    if os.getenv("ADMIN_PASSWORD", "") in ("", "admin123"):
        _log("⚠️  ADMIN_PASSWORD 仍为默认弱口令，部署请通过环境变量设置强密码。")

    procs: list[tuple[str, subprocess.Popen]] = []
    stop = threading.Event()

    def _launch(tag: str, cmd: list[str], port: int, host: str, logfile: Path) -> None:
        _log(f"[main] 启动 {tag} (监听 {host}:{port}) : {' '.join(cmd)}")
        p = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),
        )
        procs.append((tag, p))
        threading.Thread(target=_pump, args=(p.stdout, logfile, tag), daemon=True).start()

    _launch("admin", *_build_admin_cmd(args), LOGS / "admin.log")

    def _shutdown(signum, _frame) -> None:
        _log(f"\n[main] 收到信号 {signum}，正在关闭…")
        stop.set()
        for _tag, p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    admin_port = _resolve_port(args.port)
    bind_host = _resolve_host(args.host)
    # 回显统一用回环地址：0.0.0.0 只是绑定通配符，不能直接当 URL 打开
    show_host = _display_host(bind_host)
    _log(f"[main] 单端口服务已启动（监听 {bind_host}:{admin_port}）：")
    _log(f"       管理后台    : http://{show_host}:{admin_port}/admin")
    _log(f"       OpenAI 端点 : http://{show_host}:{admin_port}/v1/chat/completions  (带 Key 配额)")
    _log(f"       Responses   : http://{show_host}:{admin_port}/v1/responses         (带 Key 配额)")
    _log(f"       Claude 端点 : http://{show_host}:{admin_port}/v1/messages          (带 Key 配额，Anthropic 协议)")
    _log(f"       内嵌 /gw    : http://{show_host}:{admin_port}/gw/v1/...            (桌面登录态，无配额)")
    _log(f"       客户端 base_url :")
    _log(f"           OpenAI SDK  -> http://{show_host}:{admin_port}/v1")
    _log(f"           Claude Code -> http://{show_host}:{admin_port}   (注意：不能再带 /v1)")
    _log(f"       API Key 在后台「API Keys」页创建；Claude 端的模型名会自动按档次映射")
    if bind_host in ("0.0.0.0", "*", "::"):
        lan = _lan_ip()
        if lan:
            _log(f"       局域网访问 : http://{lan}:{admin_port}/admin")
    _log("[main] 按 Ctrl+C 停止。")

    # 主循环：子进程异常退出则整体退出，避免孤儿进程
    while not stop.is_set():
        for tag, p in list(procs):
            rc = p.poll()
            if rc is not None and not stop.is_set():
                _log(f"[main] ❌ {tag} 已退出 (code={rc})，关闭服务…")
                stop.set()
                for _t2, p2 in procs:
                    if p2 is not p:
                        try:
                            p2.terminate()
                        except Exception:
                            pass
                for _t2, p2 in procs:
                    try:
                        p2.wait(timeout=10)
                    except Exception:
                        try:
                            p2.kill()
                        except Exception:
                            pass
                sys.exit(rc if rc != 0 else 1)
        time.sleep(0.5)

    for _tag, p in procs:
        try:
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _log("[main] 已停止。")


if __name__ == "__main__":
    main()
