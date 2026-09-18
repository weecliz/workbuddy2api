"""每日签到任务：遍历活跃账号领取 100 积分。

风控要点：
  - 全部请求经 CredentialManager 注入 X-Device-Token（与桌面端一致）。
  - 账号间随机延迟 2~5s（对齐 refresh_balances 的口径）：查状态 + 领取是
    连续两个请求，零间隔连发是最机器化的形态，抖动打散等间距特征。
    每天只跑一轮，13 个号多花 ≤1 分钟无感。
  - 若任务配置了 stop_after（下次停止领取时间），到达后直接跳过，不再发领取请求，
    避免活动下线后继续请求触发上游风控。
  - 若某账号领取返回 EventEnded(1003)，自动把 stop_after 设为今天，后续不再尝试。
"""
import random
import time
from datetime import datetime

from admin.backend import AccountSession
from admin.models import Account


def run_daily_checkin(db, schedule=None) -> dict:
    now = datetime.utcnow()

    # 停止领取时间：到达则跳过
    if schedule is not None and schedule.stop_after is not None:
        if now > schedule.stop_after:
            return {"task": "daily_checkin", "skipped": "已超过停止领取时间，不再请求",
                    "stop_after": schedule.stop_after.isoformat()}

    claimed = skipped_already = failed = 0
    ended = False
    errors: list[str] = []
    first = True
    for a in db.query(Account).filter(Account.status == "active").all():
        if not first:
            time.sleep(random.uniform(2.0, 5.0))
        first = False
        try:
            with AccountSession(a.auth_json) as sess:
                st = sess.get_checkin_status()
                if st.get("today_checked_in"):
                    skipped_already += 1
                else:
                    res = sess.claim_daily_checkin()
                    if res.get("ok"):
                        claimed += 1
                    elif res.get("status") == "event_ended":
                        ended = True
                        failed += 1
                        errors.append(f"acc{a.id}:活动已结束")
                    else:
                        failed += 1
                        errors.append(f"acc{a.id}:{res.get('status') or res.get('msg')}")
                # 写回可能已刷新的 token（签到请求会触发鉴权头刷新）
                a.auth_json = sess.updated_json()
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")

    # 发现活动已结束：自动把停止时间设为今天，防止后续继续请求
    if ended and schedule is not None:
        schedule.stop_after = now
        db.commit()

    return {
        "task": "daily_checkin",
        "claimed": claimed,
        "skipped_already": skipped_already,
        "failed": failed,
        "activity_ended": ended,
        "errors": errors[:10],
    }
