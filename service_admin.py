#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""service_admin.py — 把 workbuddy2api 管理后台注册为 Windows 系统服务。

与 start_admin.bat / main.py 的关系
-----------------------------------
本文件是「服务方式」的入口，运行的是和 main.py 完全相同的东西
（admin.server:app，即 /admin + /v1/* + /gw/*），但进程模型不同：

  start_admin.bat  -> 交互式启动，依赖控制台（title/echo/pause）+ Ctrl+C
  main.py          -> supervisor，Popen 出 uvicorn 子进程，靠 Ctrl+C 整体退出
  service_admin.py -> 服务宿主，在服务进程内直接跑 uvicorn，由 SCM 控制生命周期

为什么不直接把 .bat 或 main.py 包成服务
---------------------------------------
1) .bat 不是可执行映像，无法写进服务的 ImagePath；它依赖控制台，被 SCM
   拉起时没有控制台，`pause` 会让进程永远挂在停止状态。
2) main.py 靠 SIGINT 退出。Windows 服务收不到 SIGINT，SCM 的停止请求最终
   只能落到 Popen.terminate() 上 —— 在 Windows 上那等价于 TerminateProcess
   硬杀，在途请求（含 SSE 长连接）会被当场掐断，数据库事务可能留下半截。
   这里改成同进程内跑 uvicorn：SvcStop 只置 server.should_exit = True，
   由 uvicorn 自己走优雅关闭，等请求收尾后再返回。

必须单进程单 worker
-------------------
admin/scheduler.py 的调度线程没有跨进程锁，多 worker 会让「每日签到」
并发重复执行（真打上游，有风控风险）。服务方式天然是单进程，勿改。

假死自愈（为什么需要）
----------------------
Windows 上 asyncio 的 Proactor 事件循环在并发 accept 出错时，会关闭监听
socket 且不再重新 accept（CPython `proactor_events.py:863-870`），于是出现
「进程活着、状态 RUNNING、但永远接不到连接」的假死；SCM 看不到退出，
失败恢复策略不触发。实测该状态下网关静默 11 分钟。
本文件因此做了两件事：
  1) 启动时自检并补写 SCM 失败恢复策略（历史上它可能是空的）；
  2) 起一个看门狗线程，定期对监听端口发真实 HTTP 请求，连续失败即
     主动退出进程，把「假死」转换成 SCM 能识别的「退出并重启」。
可用 ADMIN_WATCHDOG_INTERVAL（秒）与 ADMIN_WATCHDOG_FAILURES（次数）调整。

命令
----
    python service_admin.py install    注册服务并设为自动启动（需管理员）
    python service_admin.py start      启动
    python service_admin.py stop       停止（优雅关闭）
    python service_admin.py restart    重启
    python service_admin.py status     查看服务状态 + 端口探测
    python service_admin.py remove     停止并卸载（需管理员）
    python service_admin.py run        前台运行，便于排错（Ctrl+C 退出）

依赖
----
    pip install pywin32        （仅服务方式需要，运行期其余依赖同 requirements.txt）
                               install_service.bat 会在缺失时自动安装。
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径校正，必须放在任何业务 import 之前
# ---------------------------------------------------------------------------
# 服务进程由 SCM 以 LocalSystem 拉起，工作目录默认是 C:\Windows\System32。
# 本项目大量依赖「项目根目录」这条隐含路径（.env、logs/、admin/ 包、
# converter 及其几个 adapter），所以先把 cwd 和 sys.path 都校正过来。
BASE = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve()
os.chdir(BASE)
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "service.log"

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少 pywin32，无法注册为 Windows 服务。请先安装：\n"
        f'    "{sys.executable}" -m pip install pywin32\n'
    )
    raise SystemExit(3)

# ---------------------------------------------------------------------------
# 服务元信息
# ---------------------------------------------------------------------------
SVC_NAME = "workbuddy2api"
SVC_DISPLAY = "workbuddy2api Admin Gateway"
SVC_DESC = (
    "workbuddy2api 管理后台 + 托管网关 + 内嵌 converter。"
    "单进程单端口，提供 /admin、/v1/*（Key 配额网关）、/gw/*（桌面登录态网关）。"
)

# 依赖的数据库服务名。换机器请用下面命令查实际名字后改这里：
#     Get-Service | Where-Object Name -match mysql
DEPEND_SERVICE = "MySQL84"

HOST_FALLBACK = "0.0.0.0"
PORT_FALLBACK = 8790


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def _setup_logging(console: bool) -> None:
    """服务方式没有控制台，日志只能落文件；前台 run 时额外输出到终端。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        fh = RotatingFileHandler(
            str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass

    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---------------------------------------------------------------------------
# 启动前自检
# ---------------------------------------------------------------------------
def _port_in_use(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        s.close()


def _running_as_system() -> bool:
    try:
        import win32api

        return win32api.GetUserName().upper() in ("SYSTEM", "LOCAL SYSTEM", "$SYSTEM")
    except Exception:
        return False


def _preflight_warnings(host: str, port: int) -> None:
    """把 main.py 里的部署安全检查搬过来。

    服务方式没有控制台，这些提示只能落在 logs/service.log 里，
    所以每次启动都写一遍，方便排查。
    """
    secret = os.getenv("ADMIN_JWT_SECRET", "")
    if not secret or secret.startswith("workbuddy-admin-jwt-secret-please-change"):
        logging.warning(
            "未设置强 ADMIN_JWT_SECRET，正在使用代码内置的弱密钥；"
            "服务对公网可达时任何人都能伪造管理员 token，请在 .env 覆盖。"
        )
    if os.getenv("ADMIN_PASSWORD", "") in ("", "admin123"):
        logging.warning("ADMIN_PASSWORD 仍是默认弱口令 admin123，请在 .env 修改。")

    # 以 LocalSystem 运行时 %LOCALAPPDATA% 会解析到
    # C:\Windows\system32\config\systemprofile\AppData\Local（通常为空），
    # 凭据目录就找不到了。但只要 .env 里把它写成**绝对路径**即可 ——
    # SYSTEM 对用户目录本来就有读权限（实测 DACL 为完全控制），
    # 没必要把服务改成用个人账户登录。所以这里配好了就不再刷这条警告。
    if _running_as_system():
        _auth_dir = os.getenv("ADMIN_CLIENT_AUTH_DIR", "") or ""
        _ok = bool(_auth_dir) and "%" not in _auth_dir and os.path.isdir(_auth_dir)
        if not _ok:
            logging.warning(
                "当前服务以 LocalSystem 运行，而凭据目录仍是 %LOCALAPPDATA% 这类相对写法；"
                "SYSTEM 的 %LOCALAPPDATA% 指向 C:\\Windows\\system32\\config\\systemprofile\\AppData\\Local"
                "（通常是空的），所以后台「扫描本机账号 / 注入本机」和 /gw 都取不到凭据。"
                " 处理办法：在 .env 里把 ADMIN_CLIENT_AUTH_DIR 与 CODEBUDDY_AUTH_DIR 都写成绝对路径，"
                "例如 C:\\Users\\<你的用户名>\\AppData\\Local\\CodeBuddyExtension\\Data\\Public\\auth。"
                " 不需要改服务的登录账户 —— SYSTEM 对该目录本就有读权限。"
            )

    if _port_in_use(port):
        logging.warning(
            "端口 %s 已被其他进程占用，uvicorn 绑定会失败并触发服务自动重启循环。"
            " 请先停掉占用进程（或在 .env 改 ADMIN_PORT）。",
            port,
        )


# ---------------------------------------------------------------------------
# 服务宿主
# ---------------------------------------------------------------------------
class WorkbuddyAdminService(win32serviceutil.ServiceFramework):
    _svc_name_ = SVC_NAME
    _svc_display_name_ = SVC_DISPLAY
    _svc_description_ = SVC_DESC

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._server = None  # uvicorn.Server 实例，SvcStop 靠它触发优雅退出

    # SCM 发来「停止」时在分发线程里被调用，必须尽快返回，不能在这里阻塞
    def SvcStop(self):
        logging.info("收到停止请求，通知 uvicorn 优雅退出，等待在途请求收尾 …")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        srv = self._server
        if srv is not None:
            # uvicorn 的服务主循环轮询这个标志；置位后它会停止接收新连接、
            # 等待现有请求结束，再走 lifespan shutdown。
            srv.should_exit = True
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{SVC_NAME}: starting")
        _setup_logging(console=False)
        try:
            self._serve()
        except Exception:
            import traceback

            logging.error("服务异常退出：\n%s", traceback.format_exc())
            servicemanager.LogErrorMsg(traceback.format_exc())
            raise
        logging.info("服务已正常停止。")
        servicemanager.LogInfoMsg(f"{SVC_NAME}: stopped")

    def _serve(self) -> None:
        import asyncio

        import uvicorn

        from admin.config import settings

        host = settings.HOST or HOST_FALLBACK
        port = int(settings.PORT or PORT_FALLBACK)

        _preflight_warnings(host, port)

        # 回显用回环地址：0.0.0.0 只是绑定通配符，不能直接当 URL 打开
        show = "127.0.0.1" if host in ("", "0.0.0.0", "*", "::") else host
        logging.info("启动 admin.server:app  pid=%s  cwd=%s", os.getpid(), os.getcwd())
        logging.info("  管理后台    : http://%s:%s/admin", show, port)
        logging.info("  OpenAI 端点 : http://%s:%s/v1/chat/completions  (带 Key 配额)", show, port)
        logging.info("  Responses   : http://%s:%s/v1/responses         (带 Key 配额)", show, port)
        logging.info("  Claude 端点 : http://%s:%s/v1/messages          (带 Key 配额，Anthropic 协议)", show, port)
        logging.info("  内嵌 /gw    : http://%s:%s/gw/v1/...            (桌面登录态，无配额)", show, port)
        logging.info(
            "  客户端 base_url：OpenAI SDK 填 http://%s:%s/v1 ；"
            "Claude Code 填 http://%s:%s （不能再带 /v1）",
            show, port, show, port,
        )

        # 注意：这里用 Server.serve() 而不是 uvicorn.run()。
        # uvicorn.run() 会自己装信号处理器并接管进程生命周期，在服务宿主里不合适。
        # serve() 不装信号处理器，关闭完全由上面的 should_exit 控制。
        config = uvicorn.Config(
            "admin.server:app",
            host=host,
            port=port,
            log_level="info",
            log_config=None,   # 保留本文件配置的 logging，让 uvicorn 日志也进 service.log
            access_log=True,
        )
        self._server = uvicorn.Server(config)

        # 启动前自检：历史实例上失败恢复策略可能是空的（实测 Actions=()），
        # 那样一旦假死/崩溃就永远不会被拉起。服务以 LocalSystem 运行，有权限自补。
        if not _failure_actions_configured():
            logging.warning("失败恢复策略未配置，正在自动补配（否则假死后无人拉起）")
            _set_failure_actions(quiet=True)
        else:
            logging.info("失败恢复策略已配置（异常退出后 5s/15s/60s 自动重启）")

        # 监听器看门狗：Windows Proactor 的 accept 崩溃会让进程「活着但不再监听」，
        # SCM 看不到退出因而不会重启。探活失败到阈值就主动退出，交给 SCM 拉起。
        _start_listener_watchdog(port, self._stop_event)
        logging.info(
            "监听器看门狗已启动（每 %ss 探活一次，连续 %d 次失败则重启进程）",
            WATCHDOG_INTERVAL_S, WATCHDOG_FAIL_THRESHOLD,
        )

        asyncio.run(self._server.serve())


# ---------------------------------------------------------------------------
# 安装 / 卸载 / 查询
# ---------------------------------------------------------------------------
def _is_admin() -> bool:
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _service_state():
    """返回当前服务状态码；服务未安装返回 None。"""
    try:
        return win32serviceutil.QueryServiceStatus(SVC_NAME)[1]
    except Exception:
        return None


def _state_text(code) -> str:
    return {
        win32service.SERVICE_STOPPED: "STOPPED",
        win32service.SERVICE_START_PENDING: "START_PENDING",
        win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
        win32service.SERVICE_RUNNING: "RUNNING",
        win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",
        win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
        win32service.SERVICE_PAUSED: "PAUSED",
    }.get(code, str(code))


def _mysql_service_exists(name: str) -> bool:
    try:
        win32serviceutil.QueryServiceStatus(name)
        return True
    except Exception:
        return False


def _set_failure_actions(quiet: bool = False) -> None:
    """崩溃自动重启：5s / 15s / 60s，之后每天重置一次计数。

    端口被占、MySQL 没起来这类问题会让服务反复退出，有重启策略比
    手动去 services.msc 点「恢复」省事。失败只告警，不影响服务可用性。

    quiet=True 供服务自身在每次启动时调用（服务以 LocalSystem 运行，
    有权限改自己的配置），用于修复历史实例上这个配置为空的情况。

    **参数必须是 dict 而不是 tuple**：pywin32 的 ChangeServiceConfig2 对
    SERVICE_CONFIG_FAILURE_ACTIONS 要求
    {'ResetPeriod':int,'RebootMsg':str,'Command':str,'Actions':[...]}，
    传 tuple 会直接抛 TypeError。历史版本传的是 tuple，所以本函数**从未成功过**
    —— 这正是服务既没有自动重启、`Actions` 查询结果为空的原因。
    """
    scm = hs = None
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        hs = win32service.OpenService(scm, SVC_NAME, win32service.SERVICE_ALL_ACCESS)
        actions = [
            (win32service.SC_ACTION_RESTART, 5000),
            (win32service.SC_ACTION_RESTART, 15000),
            (win32service.SC_ACTION_RESTART, 60000),
        ]
        win32service.ChangeServiceConfig2(
            hs,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {"ResetPeriod": 86400, "RebootMsg": "", "Command": "", "Actions": actions},
        )
        if not _failure_actions_configured():
            raise RuntimeError("写入后自检仍为未配置，SCM 未接受该策略")
        if not quiet:
            print("[ok] 失败恢复策略：进程异常退出后 5 秒自动重启（最多连试 3 次）")
    except Exception as e:
        if not quiet:
            print(f"[warn] 设置失败恢复策略失败，可稍后在 services.msc 手动配置：{e}")
        else:
            logging.warning("设置失败恢复策略失败（服务自身无权限？）：%s: %s",
                            type(e).__name__, e)
    finally:
        if hs:
            win32service.CloseServiceHandle(hs)
        if scm:
            win32service.CloseServiceHandle(scm)


def _failure_actions_configured() -> bool:
    """当前服务是否已配置「崩溃自动重启」。用于启动时自检并补配。"""
    scm = hs = None
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        hs = win32service.OpenService(scm, SVC_NAME, win32service.SERVICE_QUERY_CONFIG)
        cfg = win32service.QueryServiceConfig2(hs, win32service.SERVICE_CONFIG_FAILURE_ACTIONS)
        return bool(cfg.get("Actions"))
    except Exception:
        return False
    finally:
        if hs:
            win32service.CloseServiceHandle(hs)
        if scm:
            win32service.CloseServiceHandle(scm)


# ---------------------------------------------------------------------------
# 监听器看门狗
# ---------------------------------------------------------------------------
# 背景：Windows 上 asyncio 的 Proactor 事件循环在并发 accept 出错时，
# proactor_events.py:863-870 会捕获 OSError 并**直接关闭监听 socket**，
# 此后不再重新 accept —— 表现为「进程活着、服务状态 RUNNING、但再也接不到
# 任何连接」的假死。SCM 看不到进程退出，因此失败恢复策略不会被触发。
# 实测触发条件：约 40 个并发连接流入即可复现（OSError [WinError 64]）。
#
# 应对：进程内自探活。连续失败即判定假死，主动退出进程，由失败恢复策略
# 在 5 秒内拉起一个全新的监听器。这是修复该缺陷唯一可靠的位置 ——
# 缺陷在 CPython 自身，不修解释器源码无法从根上消除。
WATCHDOG_INTERVAL_S = int(os.getenv("ADMIN_WATCHDOG_INTERVAL", "20"))
WATCHDOG_FAIL_THRESHOLD = int(os.getenv("ADMIN_WATCHDOG_FAILURES", "3"))


def _probe_listener(port: int, timeout: float = 3.0) -> bool:
    """对自身监听端口做一次真实 TCP 连接 + 最小 HTTP 请求。

    只做 connect 不够：假死的典型形态是监听 socket 已被关闭（connect 直接
    失败），但事件循环被长任务卡住时 connect 仍可能排进 backlog。发一个请求
    并读到任意响应字节，才能同时证明「accept 正常」且「事件循环在转」。
    """
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        s.sendall(b"GET /gw/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        return bool(s.recv(16))
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _start_listener_watchdog(port: int, stop_event) -> threading.Thread:
    """启动探活线程：连续失败达阈值就退出进程，交给 SCM 重启。

    仅在服务模式下交由 SCM 重启才有意义；前台 run 模式下同样退出，因为
    假死的服务进程留着也没有任何用处（不会自愈），不如让调用方看到退出。
    """

    def loop():
        fails = 0
        while not stop_event.is_set():
            if _probe_listener(port):
                if fails:
                    logging.info("监听器探活恢复正常（之前连续失败 %d 次）", fails)
                fails = 0
            else:
                fails += 1
                logging.warning("监听器探活失败 %d/%d（端口 %s）", fails, WATCHDOG_FAIL_THRESHOLD, port)
                if fails >= WATCHDOG_FAIL_THRESHOLD:
                    logging.error(
                        "监听器连续 %d 次无响应，判定为假死（进程存活但已不再 accept）。"
                        " 主动退出进程以触发失败恢复策略重启；"
                        " 若服务未配置失败恢复，请执行：python service_admin.py install",
                        fails,
                    )
                    for h in logging.getLogger().handlers:
                        try:
                            h.flush()
                        except Exception:
                            pass
                    os._exit(1)
            stop_event.wait(WATCHDOG_INTERVAL_S)

    t = threading.Thread(target=loop, name="listener-watchdog", daemon=True)
    t.start()
    return t


def _install() -> int:
    if not _is_admin():
        print("[ERROR] 注册服务需要管理员权限。请用 install_service.bat（右键以管理员身份运行）。")
        return 5

    deps = None
    if _mysql_service_exists(DEPEND_SERVICE):
        deps = [DEPEND_SERVICE]
    else:
        print(f"[warn] 未找到服务 {DEPEND_SERVICE}，本次不声明服务依赖。")
        print("       这意味着开机时本服务可能先于 MySQL 启动而失败重启。")

    exe_args = f'"{SCRIPT}"'
    try:
        win32serviceutil.InstallService(
            pythonClassString=f"{SCRIPT.stem}.WorkbuddyAdminService",
            serviceName=SVC_NAME,
            displayName=SVC_DISPLAY,
            startType=win32service.SERVICE_AUTO_START,
            exeName=sys.executable,
            exeArgs=exe_args,
            description=SVC_DESC,
            serviceDeps=deps,
        )
    except Exception as e:
        print(f"[ERROR] 注册服务失败：{e}")
        return 1

    print(f"[ok] 已注册服务 {SVC_NAME}，启动类型 = 自动")
    print(f'     ImagePath : "{sys.executable}" {exe_args}')
    if deps:
        print(f"     依赖服务  : {DEPEND_SERVICE}（先起数据库，再起本服务）")
    _set_failure_actions()
    return 0


def _remove() -> int:
    if not _is_admin():
        print("[ERROR] 卸载服务需要管理员权限。请用 uninstall_service.bat（右键以管理员身份运行）。")
        return 5

    state = _service_state()
    if state is None:
        print("服务未安装，无需卸载。")
        return 0

    if state != win32service.SERVICE_STOPPED:
        print("正在停止服务 …")
        try:
            win32serviceutil.StopServiceWithDeps(SVC_NAME, waitSecs=30)
        except Exception as e:
            print(f"[warn] 停止服务时出错，继续尝试删除：{e}")

    try:
        win32serviceutil.RemoveService(SVC_NAME)
    except Exception as e:
        print(f"[ERROR] 删除服务失败：{e}")
        return 1

    print(f"[ok] 已卸载服务 {SVC_NAME}")
    print("     日志文件保留在 logs/ 目录，未做清理。")
    return 0


def _status() -> int:
    state = _service_state()
    if state is None:
        print(f"服务 {SVC_NAME} 未安装。安装：python service_admin.py install")
        return 1
    print(f"服务名   : {SVC_NAME}")
    print(f"显示名   : {SVC_DISPLAY}")
    print(f"状态     : {_state_text(state)}")
    try:
        cfg = win32serviceutil.QueryServiceConfig(SVC_NAME)
        print(f"启动类型 : {cfg[0]}  (2 = 自动)")
        print(f"ImagePath: {cfg[3]}")
    except Exception:
        pass
    fa = _failure_actions_configured()
    print(f"失败恢复 : {'已配置（异常退出 5s/15s/60s 自动重启）' if fa else '未配置 —— 假死/崩溃后不会被拉起'}")
    from admin.config import settings as _s

    port = int(_s.PORT or PORT_FALLBACK)
    # 用真实 HTTP 探活，而不是仅 connect：假死的典型形态是监听 socket 已关闭，
    # 但服务状态仍是 RUNNING，只看端口会误判。
    ok = _probe_listener(port)
    print(f"探活     : {port} {'正常（HTTP 有响应）' if ok else '无响应 —— 疑似假死，请重启服务'}")
    return 0


# ---------------------------------------------------------------------------
# 前台运行（排错用）
# ---------------------------------------------------------------------------
def _run_foreground() -> int:
    import uvicorn

    from admin.config import settings

    host = settings.HOST or HOST_FALLBACK
    port = int(settings.PORT or PORT_FALLBACK)
    _setup_logging(console=True)
    _preflight_warnings(host, port)
    print(f"[run] 前台运行 mode，Ctrl+C 退出。日志同时写入 {LOG_FILE}")

    # 前台/容器方式没有 SCM 兜底，但假死后主动退出仍然正确：进程留着不会自愈，
    # 退出能让 systemd / docker restart=always / 调用方脚本重新拉起。
    _start_listener_watchdog(port, threading.Event())
    print(f"[run] 监听器看门狗已启动（每 {WATCHDOG_INTERVAL_S}s 探活，连续 "
          f"{WATCHDOG_FAIL_THRESHOLD} 次失败则重启进程）")

    # 前台模式用 uvicorn.run：它会自己装信号处理器，Ctrl+C 可优雅退出
    uvicorn.run("admin.server:app", host=host, port=port, log_level="info")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    # 关键：SCM 拉起服务进程时是**不带任何参数**的 —— 服务的 ImagePath 就是
    #     "python.exe" "service_admin.py"
    # 这种情况必须交给 pywin32 的 StartServiceCtrlDispatcher 去分派，
    # 否则进程会立刻退出，表现为「服务启动即停止、连 logs/service.log 都不生成」。
    # 这也是本文件最初的一个 bug：无参数曾被误当成 help 处理。
    if not argv:
        # SCM 以**无参数**方式拉起本进程 —— 服务的 ImagePath 就是
        #     "python.exe" "service_admin.py"
        #
        # 注意：这里**不能**用 win32serviceutil.HandleCommandLine，它源码第一段就是
        #     if len(argv) <= 1: usage()
        # 无参数只会打印帮助再退出。真正的服务分派一向由 pywin32 的宿主
        # pythonservice.exe 承担，而本环境并没有那个 exe（已确认不存在）。
        # 所以按 DebugService() 里注释所说的「pythonservice.exe 所做的事」手工复刻三步。
        # 少任何一步，现象都是「服务启动即停止、连 logs/service.log 都不生成」。
        import servicemanager
        import traceback

        try:
            servicemanager.Initialize(SVC_NAME, None)
            servicemanager.PrepareToHostSingle(WorkbuddyAdminService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception:
            # 服务方式下没有控制台，失败原因必须落盘，否则完全无从查起
            try:
                import time as _t

                with open(LOG_DIR / "service-boot.log", "a", encoding="utf-8") as f:
                    f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} 服务分派失败:\n"
                            f"{traceback.format_exc()}\n")
            except Exception:
                pass
            print(
                "\n提示：无参数模式是给服务控制管理器用的。"
                "想在终端里调试请执行：python service_admin.py run\n"
                "（在普通终端直接无参数运行，拿不到 SCM 连接是正常现象）",
                file=sys.stderr,
            )
            return 1
        return 0

    cmd = argv[0].lower()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "install":
        return _install()
    if cmd == "remove":
        return _remove()
    if cmd == "status":
        return _status()
    if cmd in ("run", "debug"):
        return _run_foreground()
    if cmd in ("start", "stop", "restart"):
        # 交给 pywin32 的标准实现（等价 sc start/stop，但不需要 sc.exe）
        sys.argv = [sys.argv[0], cmd]
        win32serviceutil.HandleCommandLine(WorkbuddyAdminService)
        return 0

    print(f"未知命令：{cmd}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
