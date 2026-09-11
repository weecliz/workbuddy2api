#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 一键拉起 workbuddy2api（单端口单进程部署）。

本项目对外只暴露「一个端口」即可：
  - 管理后台前端（index.html）与后端 API：http://127.0.0.1:8790/admin
  - 对外共享的托管网关（带 Key 校验 / 配额 / 用量记账）：http://127.0.0.1:8790/v1/chat/completions、/v1/models
  - 内嵌的独立网关 converter（本机桌面登录态直连，额外支持 /v1/responses、/v1/messages、/v1/balance）：
        http://127.0.0.1:8790/gw/v1/...

converter 已在 admin/server.py 中挂载到 /gw 前缀，因此无需再单独开 8787 端口/进程。
（若你确实需要独立运行 converter 在 8787，直接 `python converter.py --desensitize` 即可，与本脚本互不冲突。）

子进程输出实时 tee 到控制台 + logs/ 日志；Ctrl+C / 关闭终端优雅关闭。

用法:
  python main.py                  # 前台常驻，监听 0.0.0.0:8790
  python main.py --port 8790
  python main.py --host 127.0.0.1

环境变量（可选，覆盖默认；部署务必设置）:
  ADMIN_PORT / ADMIN_HOST
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


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


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


# 运行后台所需的核心依赖；当前解释器缺任一即尝试切换到带依赖的虚拟环境。
_REQUIRED_DEPS = ["pymysql", "uvicorn", "fastapi", "redis", "sqlalchemy"]


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


def _build_admin_cmd(args) -> tuple[list[str], int, str]:
    port = int(os.getenv("ADMIN_PORT", str(args.port)))
    host = os.getenv("ADMIN_HOST", args.host)
    cmd = [PY, "-m", "uvicorn", "admin.server:app",
           "--host", host, "--port", str(port), "--log-level", "info"]
    return cmd, port, host


def main() -> None:
    # 依赖自检：当前解释器缺包则自动切换到带依赖的虚拟环境（修复系统 Python 缺 pymysql 导致启动即崩退出）
    py = _interpreter_with_deps()
    if py is None:
        _log("❌ 当前 Python 缺少依赖：" + ", ".join(_REQUIRED_DEPS))
        _log("   请先安装：pip install -r requirements.txt，或激活已装好依赖的虚拟环境后再运行。")
        sys.exit(2)
    if py != sys.executable:
        _log(f"[main] 当前解释器缺依赖，自动改用虚拟环境：{py}")
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])

    ap = argparse.ArgumentParser(description="workbuddy2api 一键启动（单端口：管理后台 + 内嵌网关）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8790, help="服务端口（默认 8790）")
    args = ap.parse_args()

    # 加载项目根目录的 .env，与 admin/config.py 保持一致。
    # 不加载的话，下面基于 os.getenv 的安全告警看不到 .env 里配置的值，会误报
    # 「未设置强 ADMIN_JWT_SECRET」；子进程（uvicorn）自己会加载 .env，
    # 于是出现「实际用了强密钥、却仍告警」的矛盾现象。
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass  # 没装 python-dotenv 时静默跳过（不影响启动）

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

    admin_port = os.getenv("ADMIN_PORT", str(args.port))
    bind_host = os.getenv("ADMIN_HOST", args.host)
    # 回显统一用回环地址：0.0.0.0 只是绑定通配符，不能直接当 URL 打开
    show_host = _display_host(bind_host)
    _log(f"[main] 单端口服务已启动（监听 {bind_host}:{admin_port}）：")
    _log(f"       管理后台   : http://{show_host}:{admin_port}/admin")
    _log(f"       托管网关   : http://{show_host}:{admin_port}/v1/chat/completions  (带 Key 配额)")
    _log(f"       内嵌网关   : http://{show_host}:{admin_port}/gw/v1/...            (桌面登录态 / responses / messages)")
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
