"""X-Device-Token 提供器（Python 侧）。

复用本机 WorkBuddy 桌面端自带的 Turing Shield SDK（原生模块）取得设备风控 Token，
供 workbuddy2api 的全部后端请求注入 `X-Device-Token` 头，避免被上游风控识别为异常客户端。

实现：调用项目根目录的 `turing_helper.js`（Node 脚本），该脚本 require 桌面端的
TuringShieldSDK 原生桥接并返回 token。token 带进程内缓存（默认 10 分钟），避免每次
请求都 fork 一个 Node 进程。

失败（桌面端未安装 / SDK 不支持 / 超时）时返回 None，调用方应优雅降级（不注入该头），
绝不影响主流程。失败原因会 WARNING 一次（同一原因不重复刷屏），并做短 TTL 负缓存，
避免每个请求都重新 fork node。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# 项目根目录（admin/ 的上一级）
_ROOT = Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "turing_helper.js"

# token 缓存 TTL（秒）。Turing SDK 自身也有缓存，这里再兜一层避免频繁 fork node。
_CACHE_TTL = 600

# 失败负缓存 TTL（秒）。失败时也短时间记住，避免每个请求都 fork 一次 node
# （取 token 超时上限 25s，逐个请求都等一遍是不可接受的）。设为 0 可关闭负缓存。
_FAIL_CACHE_TTL = float(os.getenv("WORKBUDDY_TURING_FAIL_TTL", "60"))

_cache: dict = {"token": None, "ts": 0.0, "failed_at": 0.0}
_lock = threading.Lock()
_warned: set[str] = set()

_logger = logging.getLogger(__name__)


def _node_bin() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def _warn_once(msg: str) -> None:
    """同一失败原因只告警一次，避免每请求刷屏。"""
    with _lock:
        if msg in _warned:
            return
        _warned.add(msg)
    _logger.warning(
        "X-Device-Token 不可用：%s（相同原因不再重复提示；功能降级为不注入该头，主流程不受影响）",
        msg,
    )


def _remember_failure() -> None:
    """记录一次失败（负缓存）。"""
    with _lock:
        _cache["token"] = None
        _cache["failed_at"] = time.time()


def get_device_token(force: bool = False) -> str | None:
    """返回本机设备风控 Token；取不到返回 None。

    force=True 时忽略缓存（含负缓存），重新向 SDK 索取（用于排查 / 测试）。
    """
    now = time.time()
    if not force:
        with _lock:
            if _cache["token"] and now - _cache["ts"] < _CACHE_TTL:
                return _cache["token"]
            if (
                not _cache["token"]
                and _cache["failed_at"]
                and now - _cache["failed_at"] < _FAIL_CACHE_TTL
            ):
                return None

    node = _node_bin()
    if not node or not _HELPER.is_file():
        _warn_once(
            "找不到 node 可执行文件或 %s 不存在（node=%r）" % (_HELPER.name, node)
        )
        _remember_failure()
        return None

    env = dict(os.environ)
    # 不写死 SDK 目录：若用户显式设置了 WORKBUDDY_TURING_SDK_DIR 则下发，
    # 否则交给 turing_helper.js 按本机安装位置自动发现（不同用户安装目录不同）。
    # 注意：不要在此处兜底写死某个绝对路径，否则会覆盖 helper 的自动发现逻辑。

    try:
        # 注意：这里**不能**用 text=True。Windows 下 text=True 会按系统区域设置
        # （中文系统为 GBK/cp936）解码，而 node 输出的是 UTF-8；一旦 helper 往
        # stderr 写中文报错，_readerthread 会直接抛 UnicodeDecodeError，把真正的
        # 失败原因整个吞掉（只留下一堆 traceback）。改为取 bytes 后显式按 UTF-8 解码。
        out = subprocess.run(
            [node, str(_HELPER)],
            capture_output=True, timeout=25, env=env,
        )
    except Exception as exc:
        _warn_once("调用 turing_helper.js 异常：%r" % (exc,))
        _remember_failure()
        return None

    stdout = (out.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (out.stderr or b"").decode("utf-8", errors="replace").strip()

    if out.returncode != 0:
        first_line = stderr.splitlines()[0] if stderr else "(stderr 为空)"
        _warn_once("turing_helper.js 退出码 %d：%s" % (out.returncode, first_line))
        _remember_failure()
        return None

    try:
        token = (json.loads(stdout).get("token") or "").strip() or None
    except Exception:
        _warn_once("turing_helper.js stdout 不是合法 JSON：%s" % stdout[:200])
        _remember_failure()
        return None

    if not token:
        _warn_once("turing_helper.js 返回了空 token")
        _remember_failure()
        return None

    with _lock:
        _cache["token"] = token
        _cache["ts"] = time.time()
        _cache["failed_at"] = 0.0
    return token


def clear_cache() -> None:
    """清除缓存（进程内）。调试用。"""
    with _lock:
        _cache["token"] = None
        _cache["ts"] = 0.0
        _cache["failed_at"] = 0.0
        _warned.clear()
