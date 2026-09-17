# 定时任务与运维

> 本文是 README 的细节拆分：每日签到、成长任务全自动、日志与排障。

## 五、每日签到定时任务（daily_checkin）

基于 [逆向工程](./REVERSE_ENGINEERING.md) 的结论实现：自动给所有活跃账号领「每日 100 积分」，并自带 **风控保护**。

### 5.1 风控保护

- 全部请求经 `CredentialManager` 注入 `X-Device-Token`（与桌面端一致）
- 任务可配 **「下次停止领取」时间 `stop_after`**：到达后直接跳过，不再发领取请求，避免活动下线后继续请求触发上游风控
- 若某账号领取返回 `EventEnded(1003)`，自动把 `stop_after` 设为今天，后续不再尝试

### 5.2 实现位置

- `admin/backend.py` — `AccountSession.get_checkin_status()` / `claim_daily_checkin()`（用软请求，业务码非 0 不抛异常）
- `admin/scheduler.py` — `run_daily_checkin(db, schedule)`：超 `stop_after` 跳过；遇 `EventEnded` 自动置 `stop_after=今天`
- `admin/models.py` — `Schedule.stop_after` 字段（「下次停止领取」）
- `admin/routers/schedules.py` — `daily_checkin` 接入 `TASK_CHOICES` + `stop_after` 读写
- `admin/db.py` — `init_db()` 补 `schedules.stop_after` 列迁移
- `scripts/test_daily_checkin.py` — 查状态 + 仅对未领账号真实领取的验证脚本

### 5.3 配置定时任务

**后台 UI（推荐）**：管理后台「定时任务」页 → 「新建」→ 任务类型选 **每日签到领取积分**，间隔填 `1440`（每天），启用即可。选该类型会出现 **停止领取时间** 输入框（datetime-local），留空=不限制（活动结束会自动停止），填上活动结束时间更安全。

**API 示例**：`stop_after` 建议设为活动结束时间（来自 `checkin-activity-status` 的 `end_time`）：

```bash
curl -X POST http://127.0.0.1:8790/api/schedules \
  -H "X-Admin-Token: <admin_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name":"每日签到领积分","task":"daily_checkin","interval_minutes":1440,"enabled":1,"stop_after":"2026-09-15T23:59:59"}'
```

**默认即自动签到**：`start_scheduler()` 在后台启动时会 `ensure_daily_checkin(db)`——若实例里没有任何 `daily_checkin` 任务，会自动补一个「每日签到领取积分」（启用、每天）的任务。所以全新部署或已有实例都会自带定时签到配置，无需手动建。已领过当天的账号会被跳过，不会重复领取、不会误发请求。

### 5.4 验证

```bash
# 仅查状态 + 对「今日未领」账号真实领取（不影响已领账号）
PYTHONPATH=. python scripts/test_daily_checkin.py
```

已验证：活动 `开学季`，`end_time=2026-09-15 23:59:59`；未领账号各领到 100 积分，已领账号自动跳过；今日领完后调度器 `claimed=0, skipped_already=5`（不重复领、不误发请求）；`stop_after` 过期直接跳过。

---

## 五之二、成长任务全自动完成（growth_tasks）

对齐参考实现 workbuddy2api-hub 的成长中心全链路：**批量接取未接任务 → 按任务类型构造规范行为事件上报点亮 → 自动领奖入账**。接入现有定时任务框架，后台「定时任务」页任务类型选 **成长任务点亮领奖**，默认每日 1 轮（老实例升级时自动补种，无需手动建）。

### 覆盖与跳过的任务

- **自动点亮领奖**：创建设计任务(300) / 模板创建(200) / 专家助手(200) / 专家团队(150) / 技能 / 自动化 / 灵感案例 / 轻量云专家 / 主题外观 / 5 次对话 / GLM-5.2 体验 / 夜猫子(23:00-08:00)
- **明确跳过**：领养猫（归猫猫旅行管）、公益捐款（真实动作不可伪造）、Buddy 应用类（无事件分支，上报也点不亮）及一切未知任务码

### 风控口径

- 账号间 0.8s、同号上报条间 1.5s（`ADMIN_GROWTH_REPORT_GAP` 可调）；每号每日 1 轮，只补足 `target - current` 缺口，不超额刷；失败不重试（留给下一轮调度）
- 夜猫子任务硬编码 CST 23:00-08:00 时段闸门，白天自动跳过
- 总开关 `ADMIN_GROWTH_TASK_ENABLED=0` 一键停用（不发任何上游请求）
- 每号处理完回读任务进度，推进为 0 时在任务结果里记 warning（上游改版即失效的信号）

### 实现位置

- `admin/tasks/event_specs.py` — 任务规格表 + 事件构造器（纯数据，字段照抄 hub 全量）
- `admin/backend/growth.py` — 上游契约：列表/接取带 `/v2`，领奖**不带**（hub 实测口径）
- `admin/tasks/growth_tasks.py` — 编排：接取 → 点亮 → 领奖，单号异常隔离
- `tests/test_tasks_growth.py` — 状态机分支 / 跳过规则 / 夜猫时段 / 失败隔离单测

### 实测已知行为（SeeU 单号真实执行验证，2026-05）

单号真实跑通一轮：接取 8 项 / 上报 18 次 / 领奖 6 项 / **入账 800 积分**。两个实测发现：

1. **专家类任务是「记账制」而非「步进制」**：上报 3 次 `Expert_team_use_3` 只推进到
   1/3、上报 5 次 `expert_5` 只推进到 3/5，领奖返回 `task not completed`——上游对
   专家类事件**按真实使用去重计数**，与 `Expert_Philanthropy` 标 unforgeable 的
   防伪造思路一致。模板 / 主题 / 自动化等任务则是即报即满。
   **影响**：专家类单轮点不满，但每轮回读最新进度后只补缺口（`target - current`），
   多轮调度后自动收敛领奖，无需干预；这正是「失败不重试、留给下一轮」设计
   兑住的场景。
2. **接取后首次重拉的进度可能滞后**：上游记账异步，新接任务的进度在重拉清单里
   可能仍显示 0。对即报即走的任务无影响；对专家类会加剧第 1 条的进度偏差。
   同样靠每轮回读真实进度再补缺口的方式自动收敛，不需要改代码。

### 真实执行验证（如何复现）

受控单号验证脚本在 `.tmp/run_growth_real_once.py`（不进版本库），只处理指定账号：

```bash
PYTHONPATH=. python .tmp/run_growth_real_once.py SeeU   # 参数为账号 id 或昵称关键字
```

脚本执行前后各回读一次任务清单核对进度，账号表无多余写入。

---


---

## 七、日志与排障

### 推荐启动

```bash
python converter.py --desensitize --log converter.log
```

### 日志能看到什么

每次请求带唯一 ID，常见：`REQUEST BODY` · `RESPONSES → CHAT BODY` · `RESPONSES PROJECTION` · `RESPONSE BODY` · `RESPONSE RAW SSE` · `⚠️内容审核拦截`。`RESPONSES PROJECTION` 会给出投影前后消息数 / 字符数 / tool schema 压缩量。

### 常见问题

- **找不到登录文件**：桌面端没登录，或登录目录不在默认路径（macOS `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth`；Windows `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth`；Linux `~/.local/share/CodeBuddyExtension/Data/Public/auth`）。
- **401**：本地 401 = 启用了 `--api-key` 但客户端没带同 key；后端 401 = 腾讯 token 失效，重开桌面端登录。
- **响应慢**：换更快的模型如 `deepseek-v4-flash`。
- **被「敏感内容」拦截**：多为 agent runtime 文本触发（DoS / exploit / credential / sandbox / escalation / 竞争品牌词 / tool description 安全术语）。排查顺序：开 `--log` → 看 `REQUEST BODY` → Codex 看 `RESPONSES PROJECTION` → 开 `--desensitize` → 仍不稳试 `--desensitize --no-compact`。
- **签到 / 对话被风控**：确认本机装了桌面端且 `turing_helper.js` 能取到 token（`X-Device-Token` 已注入）。可 `python -c "from admin.turing_token import get_device_token; print(get_device_token())"` 验证。

---

