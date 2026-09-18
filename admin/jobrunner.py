"""后台任务执行器：把耗时批量操作从 HTTP 请求里挪出去。

为什么需要
----------
成长任务 / 猫猫旅行这类批量操作是「串行 + 限速」的：每个账号要等 accept 落库、
逐次触发、复查进度。单号实测约 40~60 秒（同号上报条间还有 1.5s 间隔），
十几个账号就是十几分钟。

这类操作如果直接在请求里同步跑：
  * 请求会一直挂到跑完，nginx 的 proxy_read_timeout（默认 60s）先到点，
    前端直接吃 504，体感就是「点了没反应」；
  * 虽然 FastAPI 给同步端点配了线程池（40），单个批量只占 1 个线程，
    不会拖垮并发，但用户体验依然很差。

做法
----
POST 立即返回 job_id，真正的执行放到守护线程里；前端拿 job_id 轮询进度。
同一个 job 键（如 "growth_run"）复用一个 runner，因此「重复点击」不会
叠起多个并发批量 —— 第二个请求会直接收到「正在执行」，而不是排队再跑一遍。

**这是关键，不是优化**：手动补跑与定时调度若同时遍历同一批账号，会两路打上游
（风控）并且并发写回同一个账号的 auth_json（丢 token 刷新）。同 key 去重 +
调用方的 `is_running` 检查合起来才是完整的互斥。

注：本模块只有内存态。进程重启后 job 丢失，前端表现为「任务不存在」；
已完成的 job 保留 _KEEP_DONE_SECONDS 秒供前端取最终结果。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Callable

#: 已完成任务在内存里保留多久（秒），供前端取最终结果
_KEEP_DONE_SECONDS = 1800

#: 一次批量任务最多记多少条明细（防止超长结果撑爆内存）；计数仍是全量的
MAX_ITEMS = 500


class Job:
    """一次后台批量执行的进度与结果。"""

    def __init__(self, key: str, total: int, title: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.title = title
        self.total = total
        self.done = 0
        self.status = "running"          # running | finished | failed
        self.phase = "准备中"
        self.started_at = datetime.utcnow()
        self.finished_at: datetime | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.items: list[dict] = []      # 每个账号的实时结果
        self.dropped = 0                 # 超出 MAX_ITEMS 被丢弃的明细条数
        self.current: str = ""           # 正在处理的账号名（心跳）
        self.beat_at: datetime | None = None
        self._lock = threading.Lock()

    def add_item(self, item: dict) -> None:
        """记录一个账号的结果（线程安全）。done 始终按真实完成数递增。"""
        with self._lock:
            self.done += 1
            if len(self.items) < MAX_ITEMS:
                self.items.append(item)
            else:
                self.dropped += 1
            self.current = ""

    def beat(self, account: str = "") -> None:
        """心跳：表示「还活着，正在处理某个账号」。

        慢账号（上游要等几秒）如果什么都不上报，前端会一直显示同一个
        进度，看起来像卡死。心跳让界面能显示「正在处理 xxx」。
        """
        with self._lock:
            self.current = account
            self.beat_at = datetime.utcnow()

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def snapshot(self, include_items: bool = False) -> dict:
        """给前端的进度快照。

        include_items=False 时不含 items：轮询进度只需要计数，把上百条明细
        每 1.5 秒回传一次纯属浪费带宽。最终结果里包含 items 的汇总视图，
        由调用方在 worker 返回值里给出。
        """
        elapsed = ((self.finished_at or datetime.utcnow())
                   - self.started_at).total_seconds()
        snap = {
            "job_id": self.id,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "elapsed_s": round(elapsed, 1),
            # done / total 恒为 int（构造与 add_item 维护），且 if self.total 已防除零，
            # 故 int() 不可能抛异常。规则按裸调用模式匹配，不做数据流分析。
            # pi-lens-ignore: unchecked-throwing-call-python
            "percent": int(self.done / self.total * 100) if self.total else 0,
            "current": self.current,
            "result": self.result,
            "error": self.error,
        }
        if include_items:
            snap["items"] = list(self.items)
            snap["dropped"] = self.dropped
        return snap


class JobRunner:
    """按 key 管理后台任务；同 key 同时只允许一个在跑。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Job | None:
        with self._lock:
            self._purge_locked()
            return self._jobs.get(key)

    def by_id(self, job_id: str) -> Job | None:
        with self._lock:
            self._purge_locked()
            for j in self._jobs.values():
                if j.id == job_id:
                    return j
        return None

    def is_running(self, key: str) -> bool:
        j = self.get(key)
        return bool(j and j.status == "running")

    def _purge_locked(self) -> None:
        """清掉过期的已完成任务，避免内存无限增长。"""
        now = datetime.utcnow()
        dead = [
            k for k, j in self._jobs.items()
            if j.status != "running" and j.finished_at
            and (now - j.finished_at).total_seconds() > _KEEP_DONE_SECONDS
        ]
        for k in dead:
            self._jobs.pop(k, None)

    def start(self, key: str, total: int, worker: Callable[[Job], dict],
              title: str = "") -> Job:
        """启动一个后台任务。

        worker 收到 Job，负责逐个处理并调用 job.add_item() 上报进度，
        返回值会作为 job.result 存下来。
        """
        job = Job(key, total, title=title)
        with self._lock:
            self._purge_locked()
            self._jobs[key] = job

        def _run() -> None:
            try:
                job.result = worker(job)
                job.status = "finished"
            except Exception as e:  # 后台线程里必须兜住，否则静默丢失
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = datetime.utcnow()

        threading.Thread(target=_run, daemon=True,
                         name=f"wb-job-{key}").start()
        return job


#: 全局单例
RUNNER = JobRunner()

#: job key：成长任务。手动补跑（/api/growth/run）与定时调度
#: （admin/scheduler.py 的 growth_tasks 分支）**共用同一把互斥钥匙**，
#: 定时侧靠 RUNNER.is_running(KEY_GROWTH) 判断手动补跑是否在跑。
#: 定义在本模块（而非路由或任务模块）是为了让两侧都能导入而不形成循环依赖。
KEY_GROWTH = "growth_run"
