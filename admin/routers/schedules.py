"""定时任务管理：列表 / 新增 / 修改 / 删除 / 启停 / 立即运行。"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.models import Schedule
from admin.scheduler import run_task
from admin.security import require_admin

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

TASK_CHOICES = ["refresh_balances", "sync_models", "daily_checkin", "cat_travel", "activity_report", "growth_tasks"]
TASK_LABELS = {
    "refresh_balances": "刷新平台总积分",
    "sync_models": "同步模型列表",
    "daily_checkin": "每日签到领取积分",
    "cat_travel": "猫猫旅行巡检",
    "activity_report": "对话活跃上报",
    "growth_tasks": "成长任务点亮领奖",
}


def _parse_stop_after(value: str | None):
    """把 ISO 字符串解析成 datetime；空 / 非法返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


class ScheduleIn(BaseModel):
    name: str = ""
    task: str = "refresh_balances"
    interval_minutes: int = 60
    enabled: int = 1
    stop_after: Optional[str] = None  # ISO 时间字符串（daily_checkin 用）


class SchedulePatch(BaseModel):
    name: Optional[str] = None
    task: Optional[str] = None
    interval_minutes: Optional[int] = None
    enabled: Optional[int] = None
    stop_after: Optional[str] = None  # ISO 时间字符串（daily_checkin 用）


@router.get("")
def list_schedules(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(Schedule).order_by(Schedule.id.asc()).all()
    now = datetime.utcnow()
    return {"items": [
        {
            "id": s.id,
            "name": s.name,
            "task": s.task,
            "task_label": TASK_LABELS.get(s.task or "", s.task or ""),
            "interval_minutes": s.interval_minutes,
            "enabled": bool(s.enabled),
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "last_result": s.last_result or "",
            "stop_after": s.stop_after.isoformat() if s.stop_after else None,
            "due_soon": bool(s.enabled and (s.next_run_at is None or s.next_run_at <= now)),
        }
        for s in rows
    ]}


@router.post("")
def create_schedule(body: ScheduleIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if body.task not in TASK_CHOICES:
        raise HTTPException(status_code=400, detail="未知任务类型")
    s = Schedule(
        name=body.name or TASK_LABELS.get(body.task, body.task),
        task=body.task,
        interval_minutes=max(1, body.interval_minutes),
        enabled=body.enabled,
        stop_after=_parse_stop_after(body.stop_after),
        next_run_at=datetime.utcnow(),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "ok": True}


@router.patch("/{sid}")
def patch_schedule(sid: int, body: SchedulePatch, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    if body.name is not None:
        s.name = body.name
    if body.task is not None:
        if body.task not in TASK_CHOICES:
            raise HTTPException(status_code=400, detail="未知任务类型")
        s.task = body.task
    if body.interval_minutes is not None:
        s.interval_minutes = max(1, body.interval_minutes)
    if body.enabled is not None:
        s.enabled = body.enabled
        if body.enabled:
            s.next_run_at = datetime.utcnow()  # 重新启用后立即可调度
    if body.stop_after is not None:
        s.stop_after = _parse_stop_after(body.stop_after)
    db.commit()
    return {"id": s.id, "ok": True}


@router.post("/{sid}/toggle")
def toggle_schedule(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    s.enabled = 0 if s.enabled else 1
    if s.enabled:
        s.next_run_at = datetime.utcnow()
    db.commit()
    return {"id": s.id, "enabled": bool(s.enabled)}


@router.post("/{sid}/run")
def run_now(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    now = datetime.utcnow()
    # task 列在库里可空（历史遗留），与 scheduler._run_one 同样做空串兜底，
    # 免得手动触发时因 None 报错。
    _run = run_task(s.task or "", db, s)
    s.last_run_at = now
    s.next_run_at = now + timedelta(minutes=s.interval_minutes or 60)
    s.last_result = __import__("json").dumps(_run, ensure_ascii=False)[:2000]
    db.commit()
    return {"id": s.id, "result": _run}


@router.delete("/{sid}")
def delete_schedule(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(s)
    db.commit()
    return {"id": sid, "ok": True}
