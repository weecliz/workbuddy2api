"""成长中心：单账号任务进度（只读）+ 手动补跑（异步）。

为什么要单独一个路由
--------------------
成长任务的编排原本只挂在定时任务里（`admin/scheduler.py` → `run_tasks`），
入口只有 `POST /api/schedules/{sid}/run`，那意味着**只能跑全量**：
无法针对单个账号补跑、看不到逐号进度、结果被 `last_result` 截断成一行摘要。

本模块把「单账号」提升为一等公民：

  - **单账号只读查询**（`GET /accounts/{id}/tasks`）：拉该号实时任务清单并分类，
    供前端展示进度与「哪些能补跑」。纯读，只有一个 token 写回。
  - **异步补跑**（`POST /run`）：立即返回 job_id，前端轮询进度。
    为什么必须异步：单号约 40~60 秒（同号事件上报条间 1.5s），十几个账号
    就是十几分钟；同步跑会先撞上 nginx 的 proxy_read_timeout(60s) → 前端吃 504，
    体感是「点了没反应」。

互斥有三层（缺一层都会出事）
----------------------------
  1. `RUNNER.start(KEY_GROWTH, ...)` —— 同 key 只留一个 job（防重复点击叠并发）；
  2. `run_growth_tasks` 内的 `_RUN_LOCK` —— 非阻塞，抢不到直接让路；
  3. `admin/scheduler.py` 执行前查 `RUNNER.is_running(KEY_GROWTH)` —— 手动补跑
     在跑时，定时任务跳过本轮，避免两路同时打上游 + 并发覆盖 `auth_json`。

**注意**：本模块的写接口会真实打上游、真实消耗账号额度，不是演练。
前端因此加了二次确认并显示将处理的账号数。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import jobrunner
from admin.db import SessionLocal, get_db
from admin.models import Account
from admin.security import require_admin
from admin.tasks import describe_account_tasks, run_growth_tasks

router = APIRouter(prefix="/api/growth", tags=["growth"])

#: 结果里保留多少条逐号明细（job.result 的 accounts 已按账号汇总，这里限的是
#: 单个账号内部的任务级 detail，供弹窗展示）
_MAX_DETAIL_ACCOUNTS = 200


class RunIn(BaseModel):
    account_ids: list[int] = []          # 空 = 全部 active
    task_codes: list[str] | None = None  # 空 = 全部可自动任务


def _resolve_accounts(db: Session, ids: list[int]) -> list[Account]:
    """校验并解析要处理的账号；ids 为空取全部 active。

    显式传了的 id 若不存在或已禁用，直接 400 —— 静默少跑几个号比报错更难查。
    """
    # pi-lens-ignore: python-sql-injection
    actives = db.query(Account).filter(Account.status == "active").all()
    if not ids:
        return actives
    by_id = {a.id: a for a in actives}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"账号不存在或未启用: {missing}")
    # 保持请求顺序，便于前端对照它的列表
    return [by_id[i] for i in dict.fromkeys(ids)]


@router.get("/accounts/{acc_id}/tasks")
def account_tasks(acc_id: int, _: bool = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """单个账号的任务进度与分类（**只读**，不做任何接取/上报/领取）。

    会实时请求上游（约 1~2 秒），故不放在账号列表里批量调用。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    data = describe_account_tasks(acc)
    # token 可能已被刷新：只写回凭据，不 commit 任何业务字段
    db.commit()
    if data.get("errors"):
        raise HTTPException(status_code=502,
                            detail=f"拉取任务清单失败: {data['errors'][0]}")
    return data


@router.post("/run")
def growth_run(body: RunIn, _: bool = Depends(require_admin),
               db: Session = Depends(get_db)):
    """手动补跑：异步执行，立即返回 job_id。

    - 同 key 已有任务在跑 → 直接返回该 job（`reused: true`），不排队、不叠并发；
    - 空 account_ids = 全部 active 账号（等价于手动触发一次定时任务）；
    - 空 task_codes = 全部可自动任务。
    """
    running = jobrunner.RUNNER.get(jobrunner.KEY_GROWTH)
    if running and running.status == "running":
        return {"reused": True, **running.snapshot()}

    if not db.query(Account).count():
        raise HTTPException(status_code=400, detail="还没有任何账号")

    accounts = _resolve_accounts(db, body.account_ids)
    ids = [a.id for a in accounts]
    tasks = body.task_codes

    def worker(job: jobrunner.Job) -> dict:
        # worker 跑在守护线程里，必须自己开 Session（不能复用请求的 db，
        # 请求返回后它已被 close）
        db2 = SessionLocal()
        try:
            job.set_phase("执行中")
            res = run_growth_tasks(db2, account_ids=ids, task_codes=tasks,
                                   on_step=job.beat)
        finally:
            db2.close()
        # 逐号明细进 job（前端弹窗用）；返回给 result 的部分保持精简
        for r in res.get("accounts", []):
            job.add_item(r)
        return {k: v for k, v in res.items() if k not in ("accounts", "details")}

    job = jobrunner.RUNNER.start(jobrunner.KEY_GROWTH, len(ids), worker,
                                 title="成长任务补跑")
    return {"reused": False, **job.snapshot()}


@router.get("/job/{job_id}")
def growth_job(job_id: str, _: bool = Depends(require_admin)):
    """查询补跑进度；`?full=1` 时附带逐号明细（默认不含，轮询省带宽）。"""
    job = jobrunner.RUNNER.by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job.snapshot(include_items=True)


@router.get("/status")
def growth_status(_: bool = Depends(require_admin)):
    """当前是否有补跑在跑（前端进入页面时用来恢复进度条）。"""
    job = jobrunner.RUNNER.get(jobrunner.KEY_GROWTH)
    if not job:
        return {"running": False}
    return {"running": job.status == "running", **job.snapshot()}
