# -*- coding: utf-8 -*-
"""启动前的数据库自检（供 start_admin.bat 等批处理调用）。

批处理没法解析 Python 的数据库配置，所以把「按 --db-type 选驱动」和
「探一下主机端口通不通」这两件事放在这里，让 .bat 只读退出码。

用法（参数与 main.py 一致的数据库参数会被识别）：
    python scripts/db_probe.py [--db-type sqlite --db-name ...]

退出码：
    0 = 驱动齐全且目标可达（SQLite 无主机端口，恒为 0）
    2 = 驱动齐全，但主机端口连不上（仅 mysql / db2）
    3 = 选定的数据库驱动没装
    4 = 配置本身有问题（比如 ADMIN_DB_TYPE 写了不支持的值）
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _apply_argv_overrides() -> None:
    """把命令行里的数据库参数写进环境变量，再让 db_config 去解析。

    复用 main.py 的解析逻辑，保证探测结果与实际启动用的配置完全一致。
    """
    sys.path.insert(0, str(ROOT))
    try:
        import main as entry
    except Exception:
        return
    try:
        args = entry._build_parser().parse_args()
        entry._apply_db_args(args)
    except SystemExit:
        pass
    except Exception:
        pass


def check() -> int:
    _apply_argv_overrides()

    try:
        from admin.db_config import db_config
    except Exception as e:
        print(f"[FAIL] 无法解析数据库配置：{e}")
        return 4

    dia = db_config.dialect

    # 1) 驱动是否齐全（sqlite 用标准库，driver_packages 为空）
    missing = []
    for pkg in dia.driver_packages:
        # ibm_db_sa 装好后 import 名可能被规范化，两种都试一下
        for mod in {pkg, pkg.replace("-", "_").lower()}:
            try:
                __import__(mod)
                break
            except ImportError:
                continue
        else:
            missing.append(pkg)
    if missing:
        print(f"[FAIL] 数据库类型 {db_config.type}（{dia.label}）缺少驱动：{', '.join(missing)}")
        print(f"       请执行：{dia.install_driver_hint()}")
        return 3

    # 2) 连通性：文件库没有主机端口，跳过
    if dia.file_based:
        print(f"[OK]   {db_config.describe()}")
        return 0

    host = db_config.host or "127.0.0.1"
    port = db_config.port or dia.default_port
    sock = socket.socket()
    sock.settimeout(2)
    try:
        rc = sock.connect_ex((host, port))
    finally:
        sock.close()
    if rc != 0:
        print(f"[FAIL] 连不上 {dia.label}：{host}:{port}（错误码 {rc}）")
        print("       请确认数据库服务已启动，且主机 / 端口 / 账号 / 密码正确。")
        return 2

    print(f"[OK]   {db_config.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(check())
