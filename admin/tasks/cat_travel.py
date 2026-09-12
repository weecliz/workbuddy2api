"""猫猫旅行巡检：每个活跃账号单趟推进一个动作（对齐 Go 版 travelOne 状态机）。

无猫 → 同意协议 + 尝试领养（+300 积分；对话量门槛未达时上游 400，
       当日不再重试，避免对上游重试轰炸）。
有猫 → 按 travel/status 分派：
       arrived   → claim 领奖（必须带 record_id）
       idle      → 未达当日名额时 depart 派出（location_id 固定 4）
       traveling → 跳过（在途）

风控要点：账号间 sleep 0.8s；单号单动作不轮询不等待；查询失败只跳过该号。
"""
import time

from admin.backend import AccountSession
from admin.models import Account
from .common import ACCOUNT_DELAY, adopt_tried_today, mark_adopt_tried, is_adopt_threshold_error

TRAVEL_LOCATION_ID = 4  # 派出地点固定 4：1~4 收益/时长区间完全相同，无最优解


def fmt_travel_eta(st: dict) -> str:
    """从 travel/status 原始响应估算回程时间（arrive_at / server_now 毫秒时间戳口径）。

    字段缺失或类型不符时返回空串——不猜格式，宁可不显示（与 88lin 实测脚本对齐，
    server_now 比本地时钟可靠）。
    """
    try:
        arrive = st.get("arrive_at")
        now = st.get("server_now")
        if not isinstance(arrive, (int, float)) or not arrive:
            return ""
        server = now if isinstance(now, (int, float)) and now else int(time.time() * 1000)
        hours = (arrive - server) / 3600000.0
        if hours <= 0:
            return ",即将到站"
        if hours >= 1:
            return f",约{hours:.0f}小时后回"
        return f",约{int(hours * 60)}分钟后回"
    except Exception:
        return ""


def run_cat_travel(db, schedule=None) -> dict:
    departed = claimed = adopted = skipped = failed = 0
    errors: list[str] = []
    details: list[str] = []
    for a in db.query(Account).filter(Account.status == "active").all():
        try:
            with AccountSession(a.auth_json) as sess:
                try:
                    buddy = sess.buddy_info()
                    if buddy is None:
                        if adopt_tried_today(a.uid or ""):
                            skipped += 1  # 当日已判定门槛未达，不再重试
                            details.append(f"acc{a.id}:防抖跳过(当日领养门槛未达,先跑活跃上报刷对话量)")
                        else:
                            sess.buddy_agreement()  # 幂等
                            try:
                                sess.buddy_first()
                                adopted += 1
                                details.append(f"acc{a.id}:领养成功(+300)")
                            except Exception as e:
                                if is_adopt_threshold_error(e):
                                    mark_adopt_tried(a.uid or "")
                                    skipped += 1
                                    details.append(f"acc{a.id}:领养门槛未达(需对话量,先跑活跃上报)")
                                else:
                                    raise
                    else:
                        st = sess.travel_status()
                        state = st.get("state")
                        name = (buddy.get("name") if isinstance(buddy, dict) else None) or "猫"
                        if state == "arrived":
                            rid = int(st.get("record_id") or 0)
                            if rid:
                                reward = sess.travel_claim(rid)
                                claimed += 1
                                details.append(f"acc{a.id}:领奖+{reward}({name})")
                            else:
                                skipped += 1  # arrived 但无 record_id，无法领奖
                                details.append(f"acc{a.id}:到站但缺record_id,无法领奖")
                        elif state == "idle":
                            if st.get("daily_limit_reached"):
                                skipped += 1  # 服务端明确名额已用完，不白撞
                                details.append(f"acc{a.id}:今日名额已用完({name})")
                            else:
                                sess.travel_depart(TRAVEL_LOCATION_ID)
                                departed += 1
                                details.append(f"acc{a.id}:已派出(地点{TRAVEL_LOCATION_ID},{name})")
                        elif state == "traveling":
                            skipped += 1
                            details.append(f"acc{a.id}:旅行中({name}{fmt_travel_eta(st)},record={st.get('record_id') or '-'})")
                        else:
                            skipped += 1  # 未知状态
                            details.append(f"acc{a.id}:未知状态{state!r}")
                finally:
                    # 无论中途是否失败，把可能已刷新的 token 写回
                    a.auth_json = sess.updated_json()
        except Exception as e:
            failed += 1
            errors.append(f"acc{a.id}:{e}")
            details.append(f"acc{a.id}:失败:{str(e)[:60]}")
        time.sleep(ACCOUNT_DELAY)
    db.commit()
    return {"task": "cat_travel", "departed": departed, "claimed": claimed,
            "adopted": adopted, "skipped": skipped, "failed": failed,
            "details": details[:20], "errors": errors[:10]}
