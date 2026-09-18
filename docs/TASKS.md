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

### 按账号维度汇总

全量跑完一轮后，结果按**账号**聚合，不再只是一行全局计数。落库的
`schedules.last_result` 与异步 job 都返回同一形状：

```jsonc
{
  "task": "growth_tasks",
  "accepted": 8, "lit": 18, "claimed": 6, "earned_credit": 800,
  "failed": 1,
  "accounts_total": 13, "accounts_failed": 1, "truncated": false,
  "accounts": [
    {"account_id": 3, "name": "SeeU", "ok": true,
     "accepted": 8, "lit": 18, "claimed": 6, "earned_credit": 800,
     "scanned": 17, "done_tasks": 6, "skipped_night": 1,
     "summary": "接8/点亮18/领6/+800", "warning": null, "error": null}
  ],
  "details": ["acc3:接8/点亮18/领6/+800"]   // 兼容旧阅读习惯的单行文本
}
```

两个要注意的点：

- **`warning`**：某号「有待办任务但一步没动」时为 `本轮无进展(上游改版?)`，
  定时任务页会显示「无进展 N」。这是上游改版的**最早信号**，比等用户报障快。
  若该号待办全是夜猫子且当前不在夜间窗口，则不告警（那是预期行为）。
- **逐任务 `detail` 默认不返回**（`include_detail=False`）。原因：`last_result`
  落库前截断 8000 字符（`scheduler.LAST_RESULT_MAX`），13 个账号 × 每号几十条
  detail 会轻易超限，截断后 JSON 不完整、前端 `JSON.parse` 失败（表现为结果栏
  只剩半截文本）。需要逐任务明细时走手动补跑的 job（存内存，不怕大）。

### 单账号手动补跑（P1）

原先前只能跑全量。现在「账号」页每行有 **做成长任务** 图标（`fa-seedling`），
点开即拉该号实时任务清单并分类：

| 分类 | 含义 | 补跑能解决吗 |
| --- | --- | --- |
| `actionable` 可补跑 | 未接取 / 未达标 | ✅ 能推进 |
| `claimable` 待领奖 | 已完成未领 | ✅ 能立刻拿分 |
| `night` 待夜间 | 夜猫子任务，不在 23:00-08:00 | ❌ 白天也点不亮 |
| `manual` 需人工 | 不可伪造（公益捐款）/ 归其它任务管（领猫） | ❌ 补跑无用 |
| `done` 已领取 | 本轮已领 | — |

分类由 `describe_account_tasks` 给出，其判定分支**与编排逐条对齐** ——
否则会出现「界面说可补跑、点了却什么都不做」。

接口（`admin/routers/growth.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/growth/accounts/{id}/tasks` | **只读**拉清单 + 分类 + summary |
| `POST` | `/api/growth/run` | 异步补跑，返回 `job_id`（空 ids = 全部启用账号）|
| `GET` | `/api/growth/job/{job_id}` | 进度轮询（默认含逐号明细）|
| `GET` | `/api/growth/status` | 是否有补跑在跑（进页面时恢复进度条）|

#### 为什么必须异步

单号约 40~60 秒（同号事件上报条间 1.5s），十几个账号就是十几分钟。同步跑会先撞
nginx 的 `proxy_read_timeout`(60s)，前端吃 504、体感是「点了没反应」。
所以 `POST /run` 立即返回 job_id，由 `admin/jobrunner.py` 在守护线程里跑，
前端 1.5s 轮询。**这是手动补跑的前置条件，只加按钮解决不了。**

#### 互斥（三层，缺一不可）

| 层 | 机制 | 防住什么 |
| --- | --- | --- |
| 1 | `RUNNER` 同 key 去重 | 两次手动补跑叠并发 |
| 2 | `growth_tasks._RUN_LOCK`（非阻塞）| 任何原因的重入；抢不到即让路，**不排队** |
| 3 | `scheduler._run_one` / `schedules.run_now` 查 `RUNNER.is_running()` | **手动 vs 定时**撞车 |

第 3 层是必需的：jobrunner 同 key 只防得住「两次手动」。若不拦，
定时任务与手动补跑会同时遍历同一批账号 —— 双倍打上游（风控面翻倍）并
并发写回同一个 `auth_json`（后写覆盖先写，**丢掉对方的 token 刷新**）。
定时侧跳过时记 `last_result={"skipped":"已有手动补跑在执行，本轮跳过"}`；
手动触发定时任务则返回 **409**。

> ⚠️ 手动补跑会**真实上报事件并领奖**（消耗该账号额度），不是演练。
> 前端已加二次确认并显示将处理的账号数。

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

