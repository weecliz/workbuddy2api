"""对话活跃上报：点亮 growth 连登 + 解锁领猫任务（first_buddy 的 chat_5 门槛）。

每号上报 N 条（默认 5，env ADMIN_ACTIVITY_REPORT_COUNT）共用同一
conversationId（模拟同一会话内 N 轮对话），条间 1.5s，requestId 各条独立。
发满后回读 streak 自检（days==0 说明上报被上游静默丢弃，通常= userId 缺失）；
无猫账号发满后立即重试领养（对话量刚补满的新状态，豁免当日防抖）。

风控口径：每号每天 1 轮（调度间隔 24h），不做多时点高频上报。
"""
import os
import time

from admin.backend import AccountSession
from admin.models import Account
from .common import ACCOUNT_DELAY, is_adopt_threshold_error, mark_adopt_tried

REPORT_GAP = 1.5  # 同一账号连续上报之间的间隔（秒）：秒发易触发风控


def activity_report_count() -> int:
    """每号每次活跃上报的条数（env ADMIN_ACTIVITY_REPORT_COUNT，默认 5）。

    领养猫（buddy/first）前置需 5 次对话（chat_5 任务），默认 5 条同一
    conversationId 内多轮上报刚好刷满门槛；配置 <=0 时回落 1。
    """
    try:
        n = int(os.getenv("ADMIN_ACTIVITY_REPORT_COUNT", "5"))
    except (TypeError, ValueError):
        n = 5
    return n if n > 0 else 1


def run_activity_report(db, schedule=None) -> dict:
    count = activity_report_count()
    reported = failed = 0
    streak_warns: list[str] = []
    errors: list[str] = []
    details: list[str] = []
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            with AccountSession(a.auth_json) as sess:
                ok = 0
                try:
                    cid = f"wb2api-{int(time.time() * 1000)}"
                    for i in range(1, count + 1):
                        sess.report_chat_activity(cid, f"{cid}-r{i}")
                        ok += 1
                        if i < count:
                            time.sleep(REPORT_GAP)
                finally:
                    a.auth_json = sess.updated_json()  # token 始终写回
                if ok < count:
                    errors.append(f"acc{a.id}:上报中断({ok}/{count})")
                    details.append(f"acc{a.id}:上报中断({ok}/{count})")
                    continue  # 未发满：streak 自检与补领养均无意义
                reported += 1
                # streak 自检（只读 oracle，失败不影响主流程）
                streak_note = ""
                try:
                    days = sess.growth_streak_days()
                    if days == 0:
                        streak_warns.append(f"acc{a.id}")  # 上报 OK 但连登为 0
                        streak_note = ",连登可疑(0,疑似静默丢弃)"
                    else:
                        streak_note = f",连登{days}天"
                except Exception:
                    streak_warns.append(f"acc{a.id}")
                    streak_note = ",连登回读失败"
                # 无猫账号：对话量刚补满 → 立即重试领养（豁免当日防抖）
                adopt_note = ""
                if sess.buddy_info() is None:
                    sess.buddy_agreement()
                    try:
                        sess.buddy_first()
                        adopt_note = ",补领养成功(+300)"
                    except Exception as e:
                        if is_adopt_threshold_error(e):
                            mark_adopt_tried(a.uid or "")
                            adopt_note = ",补领养门槛仍未达"
                        # 其他领养错误静默：上报已成功，不影响本轮结果
                details.append(f"acc{a.id}:上报{ok}/{count}{streak_note}{adopt_note}")
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")
            details.append(f"acc{a.id}:失败:{str(e)[:60]}")
        time.sleep(ACCOUNT_DELAY)
    db.commit()
    return {"task": "activity_report", "reported": reported, "failed": failed,
            "count_per_account": count, "streak_warn": streak_warns,
            "details": details[:20], "errors": errors[:10]}
