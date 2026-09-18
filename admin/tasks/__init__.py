"""后台任务实现（一个任务一个文件），由 admin.scheduler.run_task 分发调用。

    daily_checkin.py    每日签到领取积分
    cat_travel.py       猫猫旅行巡检（领养 / 派出 / 领奖状态机）
    activity_report.py  对话活跃上报（点亮连登 + 解锁领猫任务）
    growth_tasks.py     成长任务全自动完成（接取 / 点亮 / 领奖；含单账号入口）
    event_specs.py      成长任务事件规格与构造器（纯数据）
    common.py           共享风控参数与领养防抖

refresh_balances / sync_models 只是对 accounts / models 路由的两行转发，
留在 scheduler.run_task 里，不值得各开一个文件。
"""
from .daily_checkin import run_daily_checkin
from .cat_travel import run_cat_travel, fmt_travel_eta
from .activity_report import run_activity_report, activity_report_count
from .growth_tasks import (
    run_growth_tasks,
    run_growth_for_account,
    describe_account_tasks,
)

__all__ = [
    "run_daily_checkin",
    "run_cat_travel",
    "run_activity_report",
    "run_growth_tasks",
    "run_growth_for_account",
    "describe_account_tasks",
    "fmt_travel_eta",
    "activity_report_count",
]
