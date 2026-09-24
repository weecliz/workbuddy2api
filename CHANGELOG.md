# 更新日志

记录本项目的每次重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
项目目前**没有打版本标签**，所以条目用**日期**做标题（将来引入 tag 后可平行换成版本号）。

条目分类：

- **新增** Added：新功能、新端点、新脚本
- **变更** Changed：行为变化、重构、目录与配置调整
- **修复** Fixed：缺陷修复
- **安全** Security：凭据、权限、泄露相关

## [未发布]

### 变更

- **`/gw/v1/messages` 也做 Anthropic 模型名映射**（`core/converter.py`）：
  该端点此前对 `model` **完全不映射**，`claude-*` 原样发给上游。现在与后台
  `/v1/messages` 共用同一套规则（精确映射 + 档次兜底），但**未命中的名字仍原样透传**
  —— converter 没有模型白名单，保持历史行为不变。
- **定时任务风控限速（账号间零间隔修补）**：全部定时任务的账号间请求间隔梳理后，
  发现 `refresh_balances`（每小时）与 `daily_checkin`（每天）遍历账号时**零间隔连发**，
  是最机器化的上游请求形态，本次统一修补：
  - `refresh_balances`：账号间随机延迟 2~5s（首号不 sleep）。13 个号一轮多花
    ≤1 分钟，每小时跑一次无感。
  - `daily_checkin`：同样补上 2~5s 随机间隔（对齐上一条口径）。
  - **任务错峰执行**：调度线程同一轮轮询内多个任务同刻到期时（播种 / 手动触发 /
    相同 interval 都会），任务之间先随机等待 60~150s 再跑——避免同一账号在几十秒内
    被 签到→旅行→上报→成长 连续打 4 轮。选择在执行层拉开而不是给 `next_run_at`
    加随机偏移：后者会随每次调度逐日累积漂移，几个月后执行时刻绕时钟转一圈。
  - 配套改造 `_loop`：到期任务先短事务只收 id，执行阶段逐任务独立开会话——
    长错峰等待期间挂着同一连接会被 MySQL 断掉空闲连接；等待后重新检查任务
    是否仍启用 / 仍到期（等待期间可能被停用或手动触发过）。

### 新增

- **Anthropic 模型名精确映射表 `ADMIN_ANTHROPIC_MODEL_MAP`**（新增
  `core/anthropic_model_map.py`；`admin/config.py`、`admin/routers/proxy.py`、
  `core/converter.py`、`admin/server.py`）：
  原先 `claude-*` 只能按 opus / sonnet / haiku 三个**档次**关键词映射，同一档次里
  无法再分型号（新款与老款只能共用同一个目标模型）。现在可在 `.env` 里按精确名
  逐条指定，例：
  `ADMIN_ANTHROPIC_MODEL_MAP=claude-opus-4-6=glm-5.3,claude-sonnet-4-5=deepseek-v4.1-flash`。
  - **判定顺序**（两侧一致）：精确映射 → 已在白名单（仅 `/v1/messages`）→
    opus / sonnet / haiku 档次 → `/v1/messages` 落 `auto`、`/gw/v1/messages` 原样透传。
    精确映射放在白名单**之前**：它是运维显式写的，应能覆盖「名字本来就在白名单里」的情形。
  - 来源名大小写不敏感，且接受 `claude-opus-4-6[1m]` 这种带 1M 标记的写法
    （标记会被忽略 —— Claude Code 本来会在客户端剥掉它，但手写配置时常被照抄）。
  - 非法条目（缺 `=`、来源或目标为空）**不阻止启动**，只在日志里告警：
    一个手滑的逗号不该让整个服务起不来。
  - 目标名仍走白名单校验：写了白名单外的名字，请求以 `400 model_not_found` 失败，
    而不是静默改道到别的模型。
  - 两侧共用同一份配置：单端口部署时 `admin/server.py` 在挂载 `/gw` 时显式注入，
    避免 `/gw` 与 `/v1/messages` 因 `.env` 加载时序不同而表现不一致。
  - 测试：`tests/test_anthropic_model_map.py`（18 例：解析 / `[1m]` 规范化 / 查表 /
    档次顺序 / 两侧优先级 / 懒加载缓存）。

- **账号页支持按积分排序**（`admin/static/index.html`）：
  「总积分」与「剩余」两列表头改为可点按钮，各自在
  **降序 → 升序 → 默认** 之间循环切换，表头箭头指示当前列与方向。
  - 默认（不排序）保持后端返回顺序（即新账号在前），与改动前完全一致。
  - 选择持久化在 `localStorage`（`wb_acc_sort`），刷新 / 切页后保持 ——
    与模型页的 `wb_model_sort` 同一套做法。
  - **未刷过余额的账号该字段为 `null`，一律当作 `-Infinity` 沉底**：
    直接 `Number(null)` 会得到 0，把「没数据」排在「真的是 0」之前，顺序会跳。
  - 排序后自动回到第一页（否则会停在中段看不到结果）；同积分按 ID 倒序保证稳定。
  - 12 项 node 测试从 `index.html` 抽取**真实函数**验证（不重写）：
    循环顺序、null 沉底、不修改原数组、持久化、稳定排序、字符串数字。
- **会话亲和：把同一对话固定到同一账号，命中上游前缀缓存**
  （新增 `admin/affinity.py`；`admin/routers/proxy.py` 的 `_select_account`）：
  上游的前缀缓存（prompt cache）**按账号隔离**，而本项目的选号策略随余额 / 使用时间
  变化 —— 同一条长对话的上一轮在账号 A、下一轮可能落到账号 B，B 侧没有任何缓存，
  每一轮都要重新处理整个前缀（长 harness 会话动辄上万 token）。
  参考实现 hub 实测缓存率 **0% → 95.2%**。
  - **亲和键** `derive_affinity_key(messages, scope)`：取对话前两条消息
    （system + 首轮 user）—— 它们在整条对话内逐字节不变，正是缓存赖以命中的那一段；
    不同对话因首轮内容不同而自然分散。`scope` 传 API Key 的 id 做租户隔离，
    避免不同租户因首轮内容巧合相同而被绑到同一账号。
  - **绑定表** `SessionAffinity`：线程安全、滑动 TTL（默认 7200s）、容量上限
    （默认 5000，超出先清过期、再按到期时间丢最旧）。
  - **不可用即改绑**：绑定账号处在冷却 / 无余额 / 已在本轮 `exclude_ids` 里时，
    解绑后走常规选号并把新账号绑上 —— 保证请求始终发得出去，亲和只是优化而非
    可用性依赖。
  - 开关 `ADMIN_ACCOUNT_AFFINITY`（默认开，`=0` 关闭）、
    `ADMIN_ACCOUNT_AFFINITY_TTL`、`ADMIN_ACCOUNT_AFFINITY_MAX`。
  - 多进程说明：绑定表是进程内的（与参考实现一致）；多 worker 部署时每个 worker
    各持一份，效果打折但不会出错。
- **成长中心单账号手动补跑（P1）**：成长任务原先只有定时任务一个入口，
  **只能跑全量**——无法针对单个账号补跑、看不到逐号进度、结果还被截断。
  本次把「单账号」提升为一等公民（`admin/routers/growth.py`，新挂载于 `admin/server.py`）：
  - `GET /api/growth/accounts/{id}/tasks` — **只读**拉该号任务清单并分类
    （可推进 / 待领奖 / 待夜间 / 需人工 / 已领取），不接取、不上报、不领奖。
  - `POST /api/growth/run` — 异步补跑，立即返回 `job_id`；空 `account_ids` = 全部
    启用账号，空 `task_codes` = 全部可自动任务。
  - `GET /api/growth/job/{job_id}`、`GET /api/growth/status` — 进度轮询与恢复。
  - **为什么必须异步**：单号约 40~60 秒（同号事件上报条间还有 1.5s 间隔），
    十几个账号就是十几分钟；同步跑会先撞 nginx 的 `proxy_read_timeout`(60s)，
    前端吃 504、体感是「点了没反应」。这是 P1 的前置条件，只加按钮解决不了。
- **后台任务执行器 `admin/jobrunner.py`**（从参考实现移植并精简）：`Job` / `JobRunner`
  / 全局 `RUNNER`，支持 job_id、进度快照、心跳（`beat`，「正在处理 xxx」）、
  同 key 去重。未移植 `wait_for` —— 现有编排用固定 gap + 每轮回读收敛，
  不需要「轮询到就绪」，不做无调用者的死代码。
- **成长任务按账号维度汇总**：`run_growth_tasks` 返回新增 `accounts`
  （每号的 `accepted/lit/claimed/earned_credit/scanned/done_tasks/skipped_night`
  /`summary`/`warning`/`error`）、`accounts_total`、`accounts_failed`、`truncated`。
  前端定时任务页「上次结果」现按账号展示，不再只有一行全局计数。
- **有序互斥（三层）**：`RUNNER` 同 key 去重（防重复点击叠并发）+ 任务内
  `_RUN_LOCK` 非阻塞锁（抢不到即让路，不排队）+ 调度侧 `RUNNER.is_running()` 检查。
  三层缺一不可：jobrunner 只防得住「两次手动」，防不住「手动 vs 定时」——
  那会双倍打上游（风控面翻倍）并并发写回同一 `auth_json`（后写覆盖先写，
  丢掉对方的 token 刷新）。

### 变更

- **成长任务编排抽出单号入口**（`admin/tasks/growth_tasks.py`）：原来的循环体
  内联在 `run_growth_tasks` 里，无法单独调用。现拆为
  `run_growth_for_account(acc, task_codes)` + 全量遍历，**手动补跑与定时调度
  共用同一份编排逻辑** —— 参考实现的 `run_accounts` docstring 记过一次真实坑：
  绕过 accept 落库等待单独实现「单号快跑」会导致任务判不完成。两者一旦分叉，
  行为差异极难排查。同时：单任务异常改为逐条隔离（原先只护到账号级；
  领奖异常曾会中断同号剩余任务），并新增 `warning`（有待办却一步未动 →
  上游可能改版的信号，会显示在定时任务页）。
- `admin/tasks/__init__.py` 导出 `run_growth_for_account` / `describe_account_tasks`。

### 文档

- **项目宣传海报 `docs/workbuddy2api-poster.png`**：新增一张 687×1024（2x 导出 1374×2048）
  的竖版海报，用于项目介绍/分享。以火箭与发射塔为视觉主体：
  - 火箭箭体承载 `WorkBuddy 2API` 标识与「登录态 → 标准 API 网关」一句话定位；
  - 右侧光环标注三种兼容协议（`/v1/chat/completions`、`/v1/messages`、`/v1/responses`）
    与能力项（工具调用、流式 SSE、故障转移）；
  - 底部列技术栈（FastAPI / SQLAlchemy 2.0 / MySQL 8 / Redis 7 可选 / Python 3.10+）
    与五项特性（开源开放、多协议、账号池、故障转移、配额限流）。
  - 生成方式：用 HTML/CSS 手工排版后由 Chrome headless `--screenshot`（2x 缩放）
    导出 PNG；排版源文件为一次性产物，未进版本库。
- **项目宣传海报（含仓库地址）`docs/workbuddy2api-poster-v2.png`**：在第一张基础上补上
  GitHub 仓库入口，方便直接分享：
  - 品牌口号下方新增胶囊按钮「`github.com/weecliz/workbuddy2api`」（带 GitHub 图标）；
  - 页脚右侧署名行同步换成同一仓库地址；
  - 规格与第一张一致（687×1024，2x 导出 1374×2048）。
- **`docs/TASKS.md` §五之二**：补「按账号汇总」与「单账号手动补跑」两节，
  含接口清单、互斥语义、以及为什么必须异步。
- **成长任务全自动完成引擎（growth_tasks）**：把 workbuddy2api-hub 的国内版成长
  中心全链路移植进本项目——批量接取未接任务 → 按任务类型构造规范行为事件上报
  点亮 → 自动调用领奖端点入账。接入现有 Schedule 框架（任务类型
  `growth_tasks`，老实例升级时由 `ensure_growth_tasks` 幂等补种，默认每日 1 轮）。
  - **事件规格与构造器**（`admin/tasks/event_specs.py`）：TASK_SPECS 覆盖
    create_canvas / template_5 / expert_5 / Expert_team_use_3 / skill_1 /
    automation_1 / playbook_prompt / Expert_lighthouse / Hp_Appearance / chat_5 /
    Model_chat_GLM5.2 / black_cat 等；字段照抄 hub 全量不裁剪（防上游加严校验）。
  - **明确跳过**：buddy5 / RichMeow / Library（hub 无事件分支，上报 heartbeat
    点不亮）、first_buddy（领养归 cat_travel 管）、Expert_Philanthropy（真实捐款
    不可伪造）及一切未知 task_code（不盲报）。
  - **上游契约**（`admin/backend/growth.py`）：任务列表/接取带 `/v2` 前缀，
    领奖 `POST /activity/growth/tasks/{code}/claim` **不带**（hub 实测口径，
    不统一不猜测）；领奖用 `_request_backend_soft`，非 0 码只记录不中断。
  - **风控口径**：账号间 0.8s（复用 ACCOUNT_DELAY）；同号上报条间
    ADMIN_GROWTH_REPORT_GAP（默认 1.5s）；每号每日 1 轮；单任务只补足
    target-current 缺口不超额刷；失败不重试留给下一轮；black_cat 仅
    CST 23:00-08:00 点亮，白天跳过留给下一轮；总开关 ADMIN_GROWTH_TASK_ENABLED。
  - **与 activity_report 的分工**：后者继续管 streak 连登与领猫解锁，
    chat_5 进度两条链路都会推进（先到先得，无害）。
  - **实测验证**（SeeU 单号真实执行一轮）：接取 8 / 上报 18 次 / 领奖 6 项 /
    入账 800 积分；夜猫子白天正确跳过。两个实测发现（详见 README §5之二）：
    专家类任务上游按真实使用**去重计数**（上报 5 次只走 3/5），单轮点不满、
    靠每轮回读进度补缺口多轮收敛；接取后首次重拉进度可能滞后（异步记账），
    同样靠回读机制自动兑住，均无需改代码。
  - 测试：`tests/test_tasks_growth.py` 13 项（状态机分支 / 跳过规则 / 夜猫
    时段边界 / 失败隔离 / 字段完整性 / 总开关）。
- **admin 侧新增 `/health` 运维探活端点**（`admin/server.py`）：返回基本状态、
  converter 挂载状态、账号池摘要（total / active）与活跃 Key 数。**不要求鉴权**
  （探活 / 监控系统的常见约定），且不含凭据、token、账号 uid 等敏感信息。
  此前 README §2.2 声称 `GET /health` 已支持，但该端点只定义在
  `core/converter.py`（独立运行或 `/gw` 前缀下），admin 侧从未注册。
- **稳定设备指纹三头**（`core/fingerprint.py`）：向上游请求注入 `X-Machine-ID` /
  `X-Session-ID` / `X-Request-ID`，由账号 uid 纯哈希派生（`md5("<salt>:<uid>")[:36]`）。
  - **与账号绑定、与部署环境无关**：不依赖桌面端或 Turing SDK，因此容器 / Sealos
    等拿不到 `X-Device-Token` 的场景同样生效（这是本次的主要收益点）。
  - 注入点在 `core/converter.py` 的 `CredentialManager._build_headers_from()`，
    是对话 / 签到 / 猫猫旅行 / 活跃上报的**唯一共同出口**，一处改动全覆盖。
  - 算法口径与参考实现 workbuddy2api-hub 的 `wb_fingerprint.py` 逐字对齐，
    换端或混用同一号池时同一 uid 派生出**相同**设备标识
    （避免同一账号在两套程序下呈现两套设备而被判为异常登录）。
  - 开关 `ADMIN_DEVICE_FINGERPRINT`，默认 `on`（对齐 hub 的无条件注入）；
    仅作回滚逃生口，设 `off` 即恢复改动前行为。
  - 说明：`X-Machine-ID` / `X-Session-ID` 目前**没有上游校验它们的逆向证据**
    （README §1.3 只逆向到 `X-Device-Token`）。这项改动的准确定位是
    「与成熟项目行为一致的加固」，而不是「已验证上游确实校验这三个头」。
- **账号页展示设备指纹**：账号表格的 UID 列下方新增一行，显示该账号的 `machineId`
  前 10 位（悬停看完整说明，点击复制完整值）。
  - 数据由 `GET /api/accounts` 新增的 `machine_id` / `session_id` 字段提供，
    后端直接调 `core/fingerprint.derive_id()` 现算 —— 与出站请求用的是同一套函数，
    保证页面显示值与真实出站头**逐字一致**（已有测试断言这一点）。
  - **uid 缺失时显示警告标记**而不再是哈希值：这种情况所有账号会派生同一个
    指纹（`derive_id` 对空 uid 退化为固定串 `anonymous`），多账号隔离实际失效，
    需要让人一眼看到而不是被一个看似正常的哈希掩盖。
  - 点击复制只把**整数 `id`** 插进 `onclick`，不把哈希字符串拼进 HTML 属性：
    `esc()` 不转义单引号而属性用单引号包裹，拼字符串会形成注入面。
- **账号页展示 token 失效时间**：UID 列下方再增一行，按剩余时长分级着色
  （>30 天灰、<30 天橙、<24 小时红），悬停看绝对时间。
  - 展示的是 **`refreshExpiresAt`（真正下限）**：access token 临近过期会被
    `CredentialManager._is_expired` 自动刷新（提前 60s），所以 access 到期
    并不代表账号失效；只有 refresh token 也到期了才真的不能再续。
    悬停提示里同时给出 access 到期时间作参考。
  - 数据由 `GET /api/accounts` 新增的 `token_refresh_expires_ts` /
    `token_expires_ts` 提供（毫秒时间戳，前端用本地时区格式化）。
- **Codex 自由格式（custom）工具支持**（`core/responses_adapter.py`）：
  Codex 的 `apply_patch` 以 Responses 的 `type: "custom"` 声明，无 `parameters`、
  只收一段自由文本。改动前这类工具被**静默丢弃**（客户端声明了但模型从不调用）。
  - 请求侧：降级为「单个 `input` 字符串参数」的 function 工具，描述中注入
    freeform 提示与 `format.definition` 语法。
  - 响应侧：调用还原为 `custom_tool_call` + `response.custom_tool_call_input.delta/done`
    事件，并把 `{"input":"..."}` 包拆回自由文本原文。
  - 入站历史：补齐 `custom_tool_call` / `custom_tool_call_output` 输入项转换
    —— 此前多轮对话中这类历史项会静默丢失。
  - 未声明 custom 工具的请求（普通 OpenAI 客户端）**行为完全不变**。

### 修复

- **Anthropic 端点的思考内容从未转发给客户端，Claude Code 思维链恒为空**
  （`core/anthropic_adapter.py`）：请求侧早已把 `thinking` 翻成 `reasoning_effort`
  （见下条），但响应侧的 `AnthropicStreamConverter` 只认 `delta.content` 与
  `delta.tool_calls`，**从不读 `delta.reasoning_content`** —— 上游即使产出了思维链，
  `/v1/messages` 也一个 `thinking_delta` 都不发，非流式聚合的 `content` 数组里同样
  没有 thinking 块。结果是「上游思考了，客户端也看不到」。
  - **修复**：把 `reasoning_content` 转成 Anthropic 的 thinking 内容块
    （`content_block_start{type:"thinking"}` → `thinking_delta` → `signature_delta`
    → `content_block_stop`），并保证块顺序为 thinking → text → tool_use
    （Anthropic 要求 thinking 排在最前，而流式无法回退，所以正文 / 工具调用已经开始后
    才到达的 reasoning 一律丢弃）。非流式两条出口（`build_message()` 与
    `get_nonstream_response()`）同样补上 thinking 块。
  - `signature` 用固定占位值 `base64("workbuddy2api")`：官方用它校验「块由 Claude
    生成」，本项目上游是 OpenAI 协议、没有该机制，客户端回传时也会被
    `anthropic_request_to_chat` 忽略，所以只需非空，以满足客户端对「signature 必填」的
    形状要求。
  - **顺带堵住一条由本次修复才可能出现的新路径**：客户端把上一轮的 thinking 块原样
    回传时，若那条 assistant 消息**只有** thinking 块（模型在思考中途被 `max_tokens`
    截断），转换结果会是 `{"role":"assistant","content":null}` —— 不带 `tool_calls`
    的裸空 assistant 不是合法的 Chat 消息。现在直接丢弃整条消息。

- **`--port` / `--host` 被 `.env` 反过来压住，「起测试实例」会打死线上服务**
  （`main.py`）：原代码是 `int(os.getenv("ADMIN_PORT", str(args.port)))` —— 环境变量
  永远赢，显式传参只在 `.env` 没配该项时才有意义。后果：线上服务在跑时
  `python main.py --port 8791` 会去绑 `8790` 然后 bind 失败退出；线上**没在跑**时
  它反过来占住 `8790` —— 把「起个测试实例」变成「打死线上服务」。
  新增 `_resolve_port` / `_resolve_host`，口径改为「显式 CLI 参数 > `.env` > 默认值」，
  与同文件里 `--db-*` 一组参数既有口径一致。

- **工具调用配对不自愈，一次失败调用即让整条会话报废**（`core/converter.py`）：
  上游要求 `role:"tool"` 的结果消息**紧跟**请求它的 assistant 消息，中间不能有
  其他消息，否则整条请求被拒：`400 code 11148 "tool calls and tool results do not
  match, please start a new conversation and retry"` —— 注意措辞是「请开新会话」，
  意味着该对话已救不回来。两类成因：
  - **孤儿调用**：工具执行失败（参数错 / 超时 / 工具不存在）时，客户端把
    `assistant.tool_calls` 写进了历史，却永远不写回结果消息；该坏历史随后
    每一轮都被原样重放，上游对之后每条消息都返 11148。
  - **配对被打断**：并行调用时中间插入了别的消息（如 Codex 的
    `image_resize_notice` 作为 developer 消息落在两个结果之间）。

  新增两个纯函数与一个总入口（口径对齐参考实现 hub `wb_proxy.py:1942/2011`）：
  - `repack_tool_result_blocks()`：把结果块移回所属批次之后（**只重排、不删**，
    结果与相对顺序不变，干扰消息移到批次之后）。
  - `cleanup_orphan_tool_calls()`：用**同一份 id 交集**（`call_ids & result_ids`）
    对称裁剪两侧——既删「有调用无结果」的调用，也删「有结果无调用」的结果，
    因此不可能留下半截配对。
  - `prepare_outbound_body()`：出站前修复的总入口，先做配对自愈（**所有模型**），
    再做 DeepSeek 推理处理（仅 DeepSeek）。这一改动使接入点仍保持 4 处而非 8 处。

  两个函数都有 `changed` 标志，未做修改时返回原对象，**正常历史行为不变**。
- **`thinking` 未进 `/gw` 透传白名单，导致客户端意图被反向执行**
  （`core/converter.py` 的 `PASSTHROUGH_BODY_KEYS`）：
  - 客户端在 `/gw/*` 路径发 `thinking:{type:"disabled"}` 时，该字段被白名单丢掉，
    后续的 DeepSeek 档位兜底看不到「已显式关闭」，反手补上 `thinking=enabled` +
    `reasoning_effort=high` —— **客户端要求不思考，却被强制开启思考**。
  - 该缺陷在引入档位兜底后才具备危害（在那之前 `thinking` 直接被丢弃，
    不会产生反向结果），本次一并修正：白名单接纳 `thinking`。
  - 同时修正档位优先级：`thinking.effort` 先前只被读进局部变量、未写回 body，
    导致客户端显式指定的 `low` 仍被默认 `high` 覆盖。
    现为「顶层 `reasoning_effort` > `thinking.effort` > 默认 `high`」。
- **流式中间帧的全 0 usage 占位会抹掉真实用量**（`admin/routers/proxy.py`）：
  `_parse_usage()` 原先对每个带 usage 的事件**无条件覆盖**，若真值帧之后再来一个
  「字段更全但数值为 0」的占位帧，已拿到的真实 Token 就被抹成 0。
  参考实现 hub v1.4.5 实测过同一问题（GPT 系列模型的最终 Token 变成 0、生成速度缺失）。
  新增 `_take_nonzero()` 做非零优先吸纳：新值为 `None` 或「零而旧值已有正数」时保留旧值；
  `prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens` 四个字段
  各自独立判定（不互相影响）。
- **Anthropic 请求的 `thinking` 被直接丢弃，Claude Code 思维链一直为空**
  （`core/anthropic_adapter.py` + `core/converter.py`）：原实现的 docstring 写着
  「`metadata` / `thinking` → 丢弃」，客户端显式请求思考时后端仍按「不思考」应答。
  现按直接上游 xiaofan6ya/workbuddy2api 的实测口径翻译：`thinking.type=disabled`
  → 不思考；`enabled` → 有 `effort` 用 effort，否则兜底 `high`
  （后端不认 `budget_tokens`，实测传它被静默忽略）。
  - **刻意分两阶段实现**：adapter 只把意图存进临时键 `__thinking_intent`，由
    `apply_thinking_intent` 在**模型映射之后**落实。原因：`/v1/messages` 的时序是
    「先 `anthropic_request_to_chat()`，再用 `_map_anthropic_model` 把
    `claude-sonnet-4` 映射成本号池的真实模型」；若在映射前就写 `reasoning_effort`，
    即使最终落到 `glm-5.2` 这类**非 DeepSeek** 模型也会带着 DeepSeek 专属参数出站
    （该问题已在实测中发现并修正）。临时键在出站前一律清除，不泄往上游。
- **DeepSeek 缺推理档位时思维链被静默丢弃**（`core/converter.py`）：只开
  `thinking` 而不带档位，后端仍按「不思考」应答。参考实现 hub 的实测数字
  （deepseek-v4.1-flash、同一 prompt）：`enabled` 无档位 → `reasoning_tokens 0`、
  `reasoning_content` 长度 0；`reasoning_effort=high` → `37` / 长度 `117`。
  现缺档时兜底补 `high`；客户端显式给的档位**永不覆盖**，
  `disabled` / `effort=none` 照常退出（不被迫思考）。
- **`reasoning_content` 未回填，缺思维链的历史会被上游拒**（`core/converter.py`）：
  新增 `backfill_reasoning_content()`，口径对齐 hub `wb_proxy.py:2087`：
  两半条件（thinking 开启 **或** 历史已有痕迹）→ 给每条 assistant 消息补齐
  `reasoning_content`，非字符串值视为缺失，并镜像到 `reasoning` 且保证非空
  （上游校验非空，单个空格能过校验且不携带模型可见语义）。

  以上两项只对 **DeepSeek 系模型**生效（与参考实现一致），非 DeepSeek 不受影响。

- **「真实积分回写」从未生效**（`admin/routers/proxy.py`）：`_fetch_real_credits()` 里
  写的是裸名 `AccountSession(auth_json)`，但本模块只 `import backend`、从未裸导入该名字，
  运行时必抛 `NameError`；而它被函数末尾的 `except Exception` 吞掉、只在日志里留一行
  traceback —— 因此这个功能**一直是静默失效的**：上游真实用量查不到，扣费全部退回
  `COST_PER_TOKEN` 估算口径，与实际账单不符。已改为 `backend.AccountSession(...)`。
  该缺陷由 pi-lens 的 `reportUndefinedVariable` 报出（此前容易被同文件近百条 SQLAlchemy
  `Column` 类型误报淹没，误当噪音忽略——它不是类型误报，是真 bug）。
- **工具与参数的 `description` 在 Responses 路径上被全部剥掉**（`core/responses_projection.py`）：
  `SCHEMA_KEEP_KEYS` 里没有 `description`，而 `_project_tools()` 也只透传
  `name`/`parameters`/`strict`。结果是经 `/v1/responses` 与 `/gw/v1/responses`
  发出的**所有工具描述都从未到达上游**——这是既有缺陷，不是本次引入。
  描述是模型判断「何时调用、怎么填参」的主要依据，剥掉会明显降低工具调用准确率。
  现已在 schema 与工具级两个位置都完整保留。
- **「Base URL 复制」按钮点了没反应**（`admin/static/index.html`）：`copyText()` 无条件调
  `i.select()`，但 Base URL 所在元素是 `<code>` 而非 `<input>` —— `<code>` 没有 `select()`，
  第一句就抛 `TypeError`，后面的剪贴板写入与 `toast("已复制")` 全执行不到。
  现在按目标类型取文本（`input.value` 或 `textContent`）并统一走回退路径；
  `navigator.clipboard` 只在安全上下文（https / **localhost**）存在，
  局域网 `http://ip:port` 访问时为 `undefined`，原实现的 `?.` 会静默跳过写入而仍提示"已复制"，
  造成“看起来成功了其实没复制”——现在会回退到 `execCommand`，失败则明确报错。
- **上游 4xx 错误在 Chat 流式路径被吞成裸文本**（`admin/routers/proxy.py` `/v1/chat/completions`）：
  `emit_client_error` 历史实现直接 `return` 上游错误原始文本，混进 `text/event-stream`
  后 OpenAI SDK 解析不出任何事件，客户端（Pi 等）只能看到一句
  `Stream ended without finish_reason`，真实的上游 400 原因（如上下文超限）被完全吞掉。
  已改为包成 `data: {"error": {...}}` 合法 SSE 事件——OpenAI SDK 收到带 `error` 字段的
  事件会抛 `APIError` 并携带完整 body，客户端能看到真实错误。同时在 `_proxy_loop`
  流式与非流式两处补了上游错误 body 的 WARNING 日志（此前 4xx 只有 httpx 状态码行，
  body 无处可查，排查只能靠猜）。
- **用量统计「今天」在凌晨会错成昨天**：`_usDateStr()` 原用 `toISOString()`（转 UTC），
  东八区下凌晨 0:00~8:00 会算出前一天。改为按本地时区逐字段拼接。
- **`inject` 改为原子写**（`admin/routers/accounts.py`）：原先用
  `open(target, "w")` 直接覆写桌面端的**活动登录文件**，一旦中途失败（磁盘满 /
  文件被客户端占用）会留下截断的 `.info`，把用户客户端登录态弄坏且不可逆。
  改为先写临时文件再 `os.replace`；`OSError` 翻译成带上下文的 500 提示，
  明确告知原始登录态未被修改。
- **调度器 `_loop()` 的 `db` 未绑定**（`admin/scheduler.py`）：`SessionLocal()`
  自身抛异常时（数据库不可用 / 驱动问题），`db` 从未绑定，而 `except` 分支里
  还要调 `db.close()` —— 会抛 `NameError: cannot access local variable 'db'`，
  且被同一个 `except` 吞掉。后果不是调度线程挂掉（它照常睡 15 秒继续），而是
  **真实错误被掩盖**，日志上看不出到底为什么没干活。已改为先置 `db = None`
  再判空关闭。
- **`_run_one()` 的 `task=None` 类型不匹配**（`admin/scheduler.py`）：`task` 列在
  库里可空（历史遗留），而 `run_task()` 声明 `task: str`。已给空串兜底，
  被归为「未知任务类型」记入 `last_result`，不会静默失败。
  同一问题也存在于**手动触发**路径（`admin/routers/schedules.py` 的 `run_now`）
  与列表接口的 `TASK_LABELS.get(s.task, ...)`，已一并对齐。
- **`AccountSession.get_token_expiry()` 一调用就抛 `AttributeError`**
  （`admin/backend/session.py`）：它读的是 `self.cm._auth`，但 `CredentialManager`
  上**根本没有 `_auth` 属性**（真正存会话 dict 的是 `_cached`）。改用公开的
  `cm.summary()["token_expires_at"]` —— 既避免摸私有属性，也因为 summary()
  内部先 `_load_if_stale()`，外部刷新过 auth 文件时能读到新值。
- **`check_quota` 在 `credit_limit` / `credit_used` 为 NULL 时抛 TypeError**
  （`admin/security.py`）：这两列的列定义允许 NULL（旧数据 / 手工插入），
  而 `check_quota` 是每个 `/v1` 请求的必经路径。一旦某把 Key 的这两列为 NULL，
  该 Key 的**所有请求都会 500**。已统一把 NULL 视为 0：与 ORM 建行时的
  `default=0` 语义一致（无限额视为 0 额度 → 402 拒绝，即安全默认）。
- **`keys.py` 多处同类 NULL 崩溃**（`admin/routers/keys.py`）：
  `/api/keys/{id}/usage` 的 pct 除法 / `round()` / 剩余计算（三处）、
  `/api/keys` 的 `masked` 拼接（`key_prefix` 可为 NULL）、
  `/api/keys/{id}/view` 的 `_decode_key(None)`。
  已全部对齐空串 / 空值兜底。
- **`patch_key` 的非法 `credit_limit` 直接抛 500**（`admin/routers/keys.py`）：
  `float(body["credit_limit"])` 对非数字字符串抛 `ValueError`，原封不动传给
  FastAPI 变成 500。改为返回 400 并告知字段名。

### 修复

- **前端定时任务下拉漏了 `growth_tasks`，编辑即静默改坏任务类型**
  （`admin/static/index.html`）：后端 `TASK_CHOICES` 有 6 项，前端下拉只有 5 项。
  除了无法新建成长任务，**更严重的是编辑**：`data.task="growth_tasks"` 匹配不到
  任何 `<option>`，按 HTML 规范 select 落到第一项，保存即把任务类型静默改成
  「刷新平台总积分」——成长任务从此消失且无任何报错。
- **成长任务结果不可见**：`scheduleResultSummary` 缺少 `growth_tasks` 分支，
  落到 `else` 裸截断 120 字符，`lit/claimed/earned_credit` 一个都看不到。
  现新增分支（接取 / 点亮 / 领奖 / +积分 / 失败 / 无进展账号数）。
- **`last_result` 截断上限 2000 → 8000**（`admin/scheduler.py` 抽为
  `LAST_RESULT_MAX`，`admin/routers/schedules.py` 共用同一常量）：新增的按账号
  汇总在 10 个账号时就会超过 2000，截断后 JSON 不完整、前端 `JSON.parse` 失败
  （表现为结果栏只剩半截文本）。全量结果默认**不带**逐任务 `detail`
  （`include_detail=False`），避免 13 个账号轻易撑爆该上限；手动补跑的 job
  存内存，传 `True` 以支持弹窗逐任务展示。
- **`schedules/{sid}/run` 与手动补跑撞车**（`admin/routers/schedules.py`）：
  该端点原先不查运行态，会与 `/api/growth/run` 同时遍历同一批账号。现返回
  **409** 并给出可操作提示；调度线程侧则记 `last_result` 后跳过本轮。

### 安全

- **`/gw` 的 API Key 校验改为定长比较**（`core/converter.py`）：`_check_auth()`
  原先写 `token != key`，比较在第一个不同字节就返回，耗时随匹配前缀长度变化，
  构成可测量的时序旁路。改用 `hmac.compare_digest` —— 同项目的
  `admin/security.py`（密码校验、Key 校验）早已统一用该做法，此处是漏网的一处。
- **前端 5 处 XSS 注入面**（`admin/static/index.html`）：动态数据未转义就拼进
  `innerHTML`。逐处修复：
  - `toast(msg)`：`msg` 常由服务端/上游字符串拼成（`注入失败: + d.detail`、
    `测速失败(${modelId}): ${d.detail}`、备份文件路径等），是最直接的注入点。
  - 注入成功提示里的备份文件路径：本机路径直接进 toast。
  - 用量明细表头：**上游 JSON 的 key 名**直接进 `<th>`。
  - 积分包周期字段：上游返回的 `cycle_start` / `cycle_end` / `deduction_end`
    直接进 `<td>`（三处）。
  - 本机账号扫描列表的失效时间 label（含日期拼接）。

  均已过 `esc()` 转义。注：其余 `innerHTML` 站点经逐个核实为纯数字或静态 HTML
  三元表达式（如 `${v>0?'text-amber-600':''}`），无注入面，未作无谓改动。

### 变更

- **ORM 声明迁移到 SQLAlchemy 2.0 的 `Mapped[...]`**（`admin/models.py`）：
  旧式 `Column()` 在类型层面使类属性成为 `Column[X]`，类型检查器会把每一处
  `obj.attr = <X>` 与 `obj.attr` 都判为「X 不能赋给 Column[X]」。本项目累计
  **250 条**此类噪音，把真问题（如 `reportUndefinedVariable` 抓到过的
  `AccountSession` 未定义）淹没在里头。迁移后降到 **80 条（−68%）**。
  - **可空性逐字段对齐旧结构**（`Mapped[X]` = NOT NULL，`Mapped[X | None]` = NULL），
    三种方言（mysql / sqlite / postgres）的建表 DDL 与迁移前**逐字一致** ——
    老实例升级**不需要任何数据库迁移**。
  - 未新增依赖：`Mapped` / `mapped_column` 是 SQLAlchemy 2.0 原生 API，
    而 `requirements.txt` 早已是 `sqlalchemy>=2.0`。
  - 验证：6 张表 CRUD + 默认值 + 可空性 + 表达式查询共 29 项断言全通过。
  - `admin/server.py` 的 `_conv_cfg` 在 `except` 降级分支补空 dict：原先降级分支
    不给它赋值，类型检查器视其为「可能未绑定」，会对下方每个下标访问报 8 条。
    用空 dict 而非 `None`（后者会把 8 条变成 11 条 “None is not subscriptable”），
    且降级时 `_CONVERTER_EMBEDDED` 为 False、该块根本不执行，语义安全。
    至此 `reportPossiblyUnboundVariable` 清零，总报错 80 → 73。
- **用量统计新增「前一天 / 后一天」快捷切换**：放在起止日期框两侧。
  按**当前区间的跨度整体平移**（单日就是前后一天；选了「近 7 天」则整窗前后移 7 天），
  而非固定 1 天；到今日后不再向未来移动，并给出提示。
- **用量统计：日期选择加了约束**（之前完全没限制）。四重保护：
  - `max` 锁住日历弹窗不能选未来，手输也由 JS 夹回今天；
  - 起止联动：改起始→结束跟走，改结束→起始跟走（不会顶掉正在改的那一端）；
  - 跨度上限 366 天，与后端 `normalize_range` 保持一致；
  - `loadUsage()` 入口再夹一次，保证**界面显示的区间 = 实际查询的区间**。
- **用量统计：移除下方「请求明细」表**及其模型过滤框、分页器与相关 JS
  （`loadUsageDetail` / `queryUsageForAccount` / `_usDetailPage` / `_pagerNode`）。
  原因：明细与汇总走**两套独立缓存**，点「强制刷新」只刷汇总、明细仍是旧快照，
  两者不同步反而让人以为数据不对；而且上方「分账号明细」已能回答「哪个账号用了多少」。
  同时去掉账号行的点击行为（原本用于跳转明细）及「操作」列。
  **后端 `GET /api/usage/detail` 端点保留**备用（未删除）。
- **模型白名单接口 `GET /api/models/configs` 改为固定排序「启用的在前，再按倍率从低到高」**：
  原先按主键 `id ASC` 返回，启用模型散落全表（实测 `deepseek-v4.1-flash` 排在 24/29 位、第 3 页），
  后台默认只显示第 1 页，导致「启用了却看不到」。三级键：`enabled` 降序 → `credit_multiplier` 升序 → `id` 升序；
  两列均经 `COALESCE` 兜底 NULL（各方言 NULL 排序位置不一致）。只改后端一处 `order_by`，前端未动。

---

## [2026-09-15]

### 新增

- **用量统计页（上游真实用量）**：管理后台新增「用量统计」标签页，直读上游账单接口
  `/billing/meter/get-user-request-usage`，区别于「日志」页读本地 `usage_logs`（网关自己的
  估算/回写记账）——统计页是**权威口径**，包含桌面端直连等一切不经网关的调用。
  - **不写死当天**：`start` / `end` 接受 `YYYY-MM-DD` 或完整时间戳，任意区间；缺省才
    是当天。前端带 今天 / 昨天 / 近 7 天 / 近 30 天 / 本月 快捷区间，亦可手选。
  - **三种视图**：区间汇总（总请求数 / 总积分 / 账号数 / 覆盖天数）、按天与按模型
    占比条形图、分账号明细；点账号行可下钻该账号请求明细。
  - **明细可过滤分页**：按模型过滤，服务端分页（严格按 `requestTime` 降序）。
  - 新模块 `admin/backend/usage.py`（纯函数式聚合，与 HTTP 解耦便于单测）、
    新路由 `admin/routers/usage.py`。
  - 新增端点：`GET /api/usage/summary`、`GET /api/usage/detail`、`GET /api/usage/accounts`、
    `POST /api/usage/cache/clear`。
  - 区间颠倒自动交换；区间跨度上限 366 天（防一次查询翻上百页）。
- **多账号并发拉取 + 60s 短缓存**。上游只有单账号维度、无跨账号汇总接口，号池汇总
  由「按账号并发拉取（线程池，上限 8）后 merge」得到。底层 `_request_backend` 是同步
  httpx，故用线程池而非 async，否则 N 个账号会串行阻塞。原始记录与聚合结果成对缓存
  60s（上游用量本身有 30~90s 分钟级延迟，缓存不会让数据更旧），前端另有「强制刷新」
  按钮绕过缓存。单账号失败只记入 `errors`、页面上标注，不拖垮整次查询。

### 修复

- **`normalize_range` 区间颠倒时会丢掉几乎整个区间**：原先交换的是「已补过时分秒的
  解析结果」，导致 `start=09-10 23:59:59 / end=09-20 00:00:00`；改为交换原始入参后
  重新补时分秒。
- **`admin/server.py` 中 `db` 可能未绑定**：`_get_stored_hash` 的两处 `try/finally`
  在 `SessionLocal()` 自身拋异常时会抛 `NameError`，把真实的数据库错误掩盖掉；
  改为判空后关闭。

### 测试

- 新增 `tests/test_usage_stats.py`（16 个用例，不碰网络）：覆盖聚合分桶与区间夹取、
  异常字段容错、多账号合并、分页翻页/停止/截断、区间规范化（默认当天 / 任意区间 /
  颠倒交换 / 过长截断）。全套回归 36 passed。

---

## [2026-09-14]

### 新增

- **数据库可插拔：新增 IBM Db2 与 SQLite 支持，默认库改为 SQLite**。
  原先只支持 MySQL（pymysql）且连接参数写死在 `admin/config.py`。现拆成三层：
  - `admin/db_config.py`（配置中心）：全项目唯一的「数据库类型 + 连接参数」定义处。
    取值优先级 `ADMIN_DATABASE_URL`（显式连接串，兼容旧部署）>
    `ADMIN_DB_TYPE` + 分项参数（`ADMIN_DB_HOST/PORT/USER/PASSWORD/NAME/SCHEMA/OPTIONS`）>
    `DEFAULT_DB_TYPE`。连接池也可配（`ADMIN_DB_POOL_SIZE` / `MAX_OVERFLOW` / `POOL_TIMEOUT` /
    `POOL_RECYCLE` / `CONNECT_TIMEOUT` / `AUTO_CREATE` / `ECHO`）。
    自检命令 `python -m admin.db_config`（密码打码）。用户名 / 密码 / 库名均做 URL 转义
  - `admin/db_dialect.py`（方言层）：抹平 URL scheme、默认端口、标识符引用符（反引号 vs 双引号）、
    类型映射（`DATETIME`→`TIMESTAMP`、`TEXT`→`CLOB`）、系统表查询、能否自动建库 / 建 schema、
    系统表标识符大小写、是否文件库。新增一种数据库只需在此登记一条 `DbDialect`
  - `admin/db.py`：按方言准备「库 / schema / 数据文件目录」，并让增量补列支持跨方言
- **启动命令可直接指定数据库**：`main.py` 新增 `--db-type` / `--db-host` / `--db-port` /
  `--db-user` / `--db-password` / `--db-name` / `--db-schema` / `--db-options` /
  `--list-db-types`。优先级高于 `.env`，只影响本次进程，不回写配置文件；
  显式给 `--db-type` 时会忽略 `.env` 的 `ADMIN_DATABASE_URL`（避免整串 URL 顶掉分项参数）
- **`scripts/db_probe.py`**：启动前数据库自检（批处理共用）。退出码
  `0` 正常 / `2` 主机端口连不上 / `3` 驱动未安装 / `4` 配置有误。
  `start_admin.bat` 与 `install_service.bat` 改用它对症自检，不再是写死的 `import pymysql`
- **`docs/DB_SUPPORT.md`**：三种数据库的配置方式、类型映射对照表、DB2 建库与页大小要求、
  常见报错（`SQLSTATE 54010` 行长超限 / `42710` 对象已存在 / `SQLSTATE 30081N` 连不上）、
  以及新增方言的接入步骤
- `start_admin.bat` 横幅新增 `database : <类型>` 回显（`--db-type` 的
  `--db-type sqlite` 与 `--db-type=sqlite` 两种写法都识别），启动日志也回显生效配置
- **`scripts/stop_admin.bat`：一键停止脚本**（与 `start_admin.bat` 配套）。
  按端口找到实际持有 socket 的进程，再双向遍历进程树后优雅关闭。因为本服务是
  `main.py → uvicorn` 的父子结构，**只杀父进程会把子进程连同端口一起留下**，反过来
  只杀持端口的 uvicorn 又会留下 `main.py` 孤儿，所以必须两头都收。
  支持 `--port N`（默认 8790 或 `ADMIN_PORT`）、`--force`（跳过优雅阶段直接强杀）、
  `--list`（只列出占用进程不执行停止）、`--help`
- **`scripts/_proc_tree.ps1`**：`stop_admin.bat` 的进程树查询辅助，`-Mode descendants`
  取后代（深到浅）、`-Mode ancestors` 取祖先链；祖先遍历在遇到**非 python 父进程时即停**，
  避免顺着 `explorer.exe` 一路往上把用户自己的终端/编辑器也杀掉

### 变更

- **默认数据库由 MySQL 改为 SQLite**（`DEFAULT_DB_TYPE = "sqlite"`）。
  零依赖、无需安装数据库服务，克隆下来 `python main.py` 直接就能跑，
  数据落在 `./data/workbuddy_admin.db`。`.env` 与 `.env.example` 也改为 SQLite 生效、
  MySQL / DB2 两段保留为注释块（启用即切换）。
  **生产环境请在 `.env` 里显式写 `ADMIN_DB_TYPE`，不要依赖兜底值**
- **依赖自检按方言走**：选 `sqlite` 不再要求安装 `pymysql`，选 `db2` 才要求 `ibm_db_sa`
- `admin/models.py` 长文本列改为跨方言写法：`Text` → `Text().with_variant(CLOB(), "db2", "ibm_db_sa")`；
  `api_keys.key_full`（`VARCHAR(2048)`）在 DB2 上改用 `CLOB`——该列只用于后台展示、不参与查询条件，
  而 2048 字节的 VARCHAR 在 DB2 默认 4K 页下易触发行长超限（`SQLSTATE 54010`）
- `admin/config.py` 不再写死连接串，改为透出配置中心的解析结果（`DATABASE_URL` 用法保持不变）
- `service_admin.py` 的 Windows 服务依赖名按数据库类型推断（mysql → `MySQL84`、db2 → `DB2-0`、
  sqlite → 不声明），`ADMIN_DB_SERVICE_NAME` 可覆盖
- `.gitignore` 忽略 `data/` 与 `*.db` / `*.db-journal` / `*.db-wal` / `*.db-shm` / `*.sqlite*`
- `requirements.txt` 注明驱动三选一：`pymysql`（MySQL）/ `ibm_db_sa`（DB2）/ 无需安装（SQLite）

### 修复

- **日志里的 emoji 会打断启动**：`main.py` 的 `_log` 直接 `print` 含 `⚠️` / `❌` 的文案，
  在 Windows 默认的 GBK 控制台下抛 `UnicodeEncodeError`（`'gbk' codec can't encode character '\u26a0'`），
  把启动流程整个打断——只要终端是 GBK 且走到了「默认弱口令 / 弱密钥」告警分支就会复现。
  现改为编码降级写入：先正常打印，编码不支持时替换为可用字符，再不行则忽略，
  确保「日志打不出来」绝不影响启动
- **补列迁移在非 MySQL 方言下会失败**：`_ensure_column` 原先写死 MySQL 的
  `INFORMATION_SCHEMA.COLUMNS` 查询与 `TINYINT/DATETIME` 等类型名，DB2 下静默失效。
  现改为按方言查系统表（DB2 走 `SYSCAT.COLUMNS`、SQLite 走 `PRAGMA_TABLE_INFO`），
  类型优先从 ORM 模型编译取得（保证「建表」与「补列」得到同一类型），
  并自动省略 DB2 LOB 类型不支持的 `DEFAULT` 子句
- `create_engine` 在无 `connect_args` 时传 `None` 会抛 `ConnectArgumentsNotSupported`
  （SQLAlchemy 2.0），改为条件性传入
- 驱动未安装时 `create_engine` 抛的是 `NoSuchModuleError` 而非 `ModuleNotFoundError`，
  原先的捕获漏了这一类，现两类都兜并给出确切的 `pip install` 提示
- `start_admin.bat` 横幅识别不出 `--db-type`：批处理 `for` 块内 `%PREV%` 是**解析期**展开的，
  「前一个参数」的写法必须 `setlocal EnableDelayedExpansion` 配合 `!PREV!`。
  同时 `if "%ERRORLEVEL%"=="N"` 写在括号块内同样是解析期展开、永远是旧值，
  必须改用裸的 `if errorlevel N` 形式。两处均已修正（否则 `--db-type db2` 时仍会报「ready」而不是「驱动缺失」）
- **`start_admin.bat` 横幅显示的数据库类型与实际启动的不一致**：横幅只读进程环境变量里的
  `ADMIN_DB_TYPE`，从不读 `.env`，于是 `.env` 里写的 `sqlite` 被无视、横幅恒显示兜底的 `mysql`。
  现按与 `main.py` 相同的优先级解析：命令行 `--db-type` > 环境变量 > `.env` > `DEFAULT_DB_TYPE`。
  解析 `.env` 时用 `eol=#` 跳过整行注释，并按「后出现的有效行覆盖先出现的」处理重复声明
  （`.env` 里通常是「1 个生效行 + 若干注释掉的备选行」），行内注释与引号也会被剥掉
- **`python main.py` 报「缺少依赖」但项目 `.venv` 其实是好的**：`start_admin.bat` 定位解释器时
  先执行 `where python` 再找项目 venv，导致 PATH 上的裸系统 Python（没装 fastapi/sqlalchemy）抢先命中。
  现调整顺序为「项目 venv → 其他已知位置 → PATH」，并在注释里说明为何顺序不能反
- **`install_service.bat` / `uninstall_service.bat` / `start_converter.bat` 也会挑错解释器**：
  与 `start_admin.bat` 同一个缺陷（`where python` 排在项目 venv 之前），三个脚本一起调整了顺序
- **Ctrl+C 停止服务时被误报成崩溃**：`main.py` 被中断退出码是 `-1`（或窗口关闭的 `-1073741510`），
  但 `start_admin.bat` 把任何非 0 退出码都当成失败，打出一大段 `Common causes` 排错提示，
  让人以为服务挂了——实际上那正是前台运行的**正常**停止方式，日志里也早已 `Application startup complete`。
  现在这两种退出码单独识别为「Stopped (interrupted)」，不再误报；
  同时那段排错提示本身也过时了（还在写「MySQL not running」、只提 `ADMIN_DATABASE_URL`），
  改为按当前方言（sqlite / mysql / db2）给出各自可能的原因，并附上
  `python -m admin.db_config` 与 `python scripts\db_probe.py` 两条自查命令
- **模型配置页「全部启用 / 全部禁用」按钮点了没反应**：前后端契约不一致 ——
  后端 `POST /api/models/batch-toggle` 按 `body.model_ids` 逐个更新，而该字段默认空列表；
  前端按钮只发 `{enabled, level}`（本意是"全量"），于是循环体一次都不执行、恒返回
  `{"updated": 0}`。HTTP 是 200、前端只提示"启用了 0 个模型"，所以表现为"按钮不起作用"。
  现改为：`model_ids` 为空即作用于该 `level` 下的全部模型，给了列表则只更新列表内的；
  只统计真正发生变化的行数，重复点击同方向时自然返回 `updated=0`。返回值补充 `level` 便于排查。
  前端同时补上二次确认，并把 `updated=0` 单独提示为「已处于该状态，无需变更」，
  不再让"合法空操作"看起来像失败
- **网关 `/v1/models` 丢弃模型能力与上下文元信息**：该端点原先只返回
  `id`/`object`/`owned_by`/`name`/`credit_multiplier` 五个字段，而上游与内嵌 converter
  的 `/v1/models` 一致提供 14 个。下游因此拿不到 `max_input_tokens`，依赖此字段判断
  上下文窗口的客户端会退回默认值并过早压缩上下文（Claude Code 不受影响，它走
  `/v1/messages/count_tokens`）；拿不到 `supports_tool_call` / `supports_reasoning` /
  `supports_images` 则无法按能力路由或关闭功能。数据本就在 `models_raw` 里，只是未回传。
  现补齐 `created`/`credits`/`description`/`supports_*`/`max_*_tokens`/`vendor`，
  与 `core/converter.py` 的 `/v1/models` 字段集完全对齐（各 14 个，零差异），消除两个
  端点的结构漂移

- **Claude Code 多轮工具调用后报 `11148 tool calls and tool results do not match`**：
  `anthropic_adapter._convert_anthropic_message` 处理 user 消息时，若同一条消息里同时
  含 `tool_result` 与文本块（Claude Code 常附带的 `<system-reminder>`），原实现用
  `insert(0, ...)` 把文本插到 tool 消息前面，转换后的序列变成
  `assistant(tool_calls) → user(文本) → tool`，破坏 OpenAI 协议要求的「带 tool_calls 的
  assistant 后必须紧跟 role=tool」约束，腾讯后端据此判定序列断裂并整条拒绝。一旦对话历史
  累积出这种消息，之后每轮请求都会带着坏结构重发，故表现为「前几个任务正常、第 3 个开始
  报错、点继续一直报同一个错」。改为把夹带文本放到 tool 消息之后（语义上用户的话本就在
  工具结果之后），并跳过空文本，序列恢复为 `assistant(tool_calls) → tool → user`。补
  `tests/test_anthropic_adapter.py` 锁定该顺序
- **网关假死自愈（监听器看门狗）**：Windows 上 asyncio 的 Proactor 事件循环遇到并发
  `accept` 出错时，会关闭监听 socket 且不再重新 accept（`proactor_events.py:863-870`）。
  结果是**进程活着、服务状态仍是 RUNNING、但再也接不到任何连接**。SCM 看不到进程退出，
  失败恢复策略因此不会触发——实测该状态下网关挂了 11 分钟、期间零请求：
  日志有 `Accept failed on a socket` / `OSError: [WinError 64]`，之后服务完全静默。
  现增加看门狗线程：每 `ADMIN_WATCHDOG_INTERVAL`（默认 20s）对 `GET /gw/health`
  发起真实 TCP 探活，连续 `ADMIN_WATCHDOG_FAILURES`（默认 3）次失败即记录诊断并
  主动退出进程，交由服务管理器在 5s 内拉起一个全新监听器
- **失败恢复策略其实从未生效**：`service_admin.py install` 会打印「5 秒自动重启」，
  但 SCM 侧实际为空（实测 `Actions = ()`、`ResetPeriod = 0`），所以进程真崩溃也不会
  被拉起。现在服务每次启动自检该配置并补写（服务以 LocalSystem 运行，有权限改自己），
  `service_admin.py status` 也改为如实显示配置状态，不再假定成功
- **真实积分回写不再「每请求一线程」**：`_fetch_real_credits` 原先对每个估算请求起一个
  `threading.Thread` 并在其中 `sleep(60)`，等于「每请求占一个线程 60 秒」。一旦上游开始
  不回传 `credit` 字段，QPS 上到两位数就是几百个并发线程。改为有界队列（2048）+ 单
  worker：worker 按最早到期时间休眠后逐条回写，队列满则丢弃并告警——估算值本身已落库，
  回写只是把估算修正为真实值，丢一条不影响记账
- **`service_admin.py status` 改用真实 HTTP 探活**：原先只做 TCP connect，假死状态下
  会误判为健康；现在会明确报出「无响应 —— 疑似假死」
- **后台页面依赖全部本地化**：`/admin` 原先从 `cdn.tailwindcss.com` 与 `cdn.jsdelivr.net`
  拉 Tailwind 与 FontAwesome。一旦机器无外网出口、或系统代理（如 `127.0.0.1:10808`）未开，
  两个请求双双 `ERR_PROXY_CONNECTION_FAILED`，页面退化成无样式裸 HTML——登录框掉进页头、
  统计卡片竖排、Tab 与表格全部走形。现改为引用仓库内 `admin/static/vendor/` 的
  `tailwind.min.js`（Play CDN 3.0.0 构建）与 FontAwesome 6.5.2（CSS + woff2），
  后台在内网 / 离线 / 代理异常环境下均正常渲染，也不再向第三方发出请求

### 文档

- **新增 `AGENTS.md` 作为跨 AI 工具的规则单一真相源**：此前项目约定只散落在
  `README.md` 与 `CHANGELOG.md` 里，AI 编程工具打开仓库时读不到精简的项目上下文，
  容易重复踩已知的坑（如把 11128 内容审核误判为渠道故障）。新增根目录 `AGENTS.md`，
  涵盖项目定位、技术栈、常用命令、代码结构、提交与日志约定、API 契约（两套鉴权与
  base_url 写法）、已知坑位与禁止事项。该文件对外公开，不含凭据、代理端口与本机路径。
- **新增 `CLAUDE.md` / `GEMINI.md` 两个桥接文件**：Claude Code 只读 `CLAUDE.md`、
  Gemini CLI 只读 `GEMINI.md`，二者均不读 `AGENTS.md`。桥接文件各自只有一行
  `@AGENTS.md` 导入语法（启动时把 `AGENTS.md` 展开进上下文）加说明注释，避免内容
  各写一遍而漂移。刻意**不**创建 `CODEBUDDY.md`：CodeBuddy 的逻辑是「根目录存在
  `CODEBUDDY.md` 就不读 `AGENTS.md`」，创建反而会破坏其自动回退；同理不建
  `QWEN.md`（Qwen Code 已原生加载 `AGENTS.md`，建了会重复加载）

---

## [2026-09-12]

### 新增

- **猫猫旅行巡检 + 对话活跃上报**：两个新的定时任务（`cat_travel` / `activity_report`），
  契约对齐 Go 版实现——无猫自动领养（+300 积分）、按 `travel/status` 派出与领奖、
  每号上报 N 条活跃事件点亮连登并解锁领养门槛
- **账号列表「活跃」列**：展示成长中心连登天数（复用余额刷新的同一会话，无额外请求开销）
- **旅行中明细回程 ETA**：按上游 `arrive_at` / `server_now` 毫秒口径估算，字段缺失就不显示
- **OAuth 一键加号**；客户端身份按凭据 `domain` 推断（workbuddy / codebuddy 两套 UA）
- **前端引号注入修复**

### 变更

- **代理重试骨架收敛**：五个 `/v1/*` 端点（chat / responses / anthropic，各分流式与非流式）
  原本各持一份重复的重试循环，统一收敛为 `_proxy_loop`；协议差异经
  `make_consumer` / `emit_client_error` / `emit_exhausted` 三个回调注入。
  附带行为统一：非流式也走共享连接池并记录延迟、异常分支的错误分类更准确
- **`backend.py` 拆包**：271 行单文件 → `admin/backend/`（http / session / checkin / growth），
  对外 API 100% 兼容
- **调度器任务实现移出**：`admin/scheduler.py` 413 → 139 行，任务实现移到
  `admin/tasks/`（一个任务一个文件），调度框架只管轮询与分发
- **项目目录归拢**：新增 `core/`（内核模块）、`docs/`（部署文档）、`deploy/`（Docker）、
  `examples/`（客户端接入示例）、`scripts/`（本机脚本）；`main.py` 与
  `service_admin.py` 作为入口留在根目录
- **仓库脱离 fork 关系**：本仓库原为 `xiaofan6ya/workbuddy2api` 的 fork，
  现重建为**独立仓库**（提交历史完整保留，URL 不变）。GitHub 上不再显示
  「领先/落后上游」的对比条与「同步复刻」按钮
- **测试固化**：`tests/` 下 20 项（代理重试骨架 11 + 猫猫旅行状态机 9）

### 修复

- **session 死亡改三振机制**：连续命中死亡标记 3 次才禁用账号，前 2 次只做 10 分钟冷却；
  纯网络抖动不再被误判为 session 死亡（改为 `server` 类，5 次才冷却且永不禁用）

### 安全

- **`.env` 移出版本库**：它此前已被提交并推送到公开仓库，内含后台密码与
  converter API Key。文件已从 git 移除（本地保留），但**历史提交里的旧凭据无法靠删除消除，
  这两个值必须轮换**

---

## [2026-09-11]

### 新增

- **`/v1/messages`（Anthropic 协议）**：带 API Key 配额与用量记账，让 Claude Code 一类
  只会说 Anthropic 协议的客户端直接吃号池
- **Windows 服务宿主 `service_admin.py`** 与安装 / 卸载脚本：以系统服务方式跑
  `admin.server:app`，由 SCM 管理生命周期
- **部署文档与镜像**：`DEPLOY_SEALOS.md`、`DEPLOY_WINDOWS.md`，以及 admin 独立部署镜像
  `Dockerfile.admin`（单端口单进程）

### 修复

- 内嵌 `/gw` 凭据初始化；启动回显补齐端点清单
- 启动地址显示与设备 Token 获取优化
- 模型配置页支持按倍率排序（PR #3）

### 文档

- README 补充 `/v1/responses` 与 `/v1/messages` 网关路由及 Anthropic 接入说明

---

## [2026-09-03]

### 修复

- 可用积分统计改用**当前周期剩余额度**（原来用的是账号层级总剩余，体验版用完后仍显示有额度）
- `turing_helper.js` 改为 SDK 目录自动发现，不再写死 `D://workbuddy`，
  修复 TuringShieldSDK 在不同机器上的加载失败

---

## [2026-09-02]

- 初始提交：workbuddy2api 网关与 admin 管理后台
- 添加项目截图（`images/`）并修正 `.gitignore`（不再误忽略图片资源）

---

## 怎么维护这份日志

1. **什么时候写**：每次合并一个有意义的功能 / 修复 / 重构，就往顶部当前日期那一节里加一行。
   不要等到发版时再回忆——那时候最容易漏。
2. **写给谁看**：用户和未来的自己。写"变了什么、为什么变"，不要只贴 commit 标题。
   例如不写「改 proxy」，而写「五个端点的重试循环收敛为一份，新增端点不用再复制重试逻辑」。
3. **换天时**：新的一天就在最上面加一节 `## [YYYY-MM-DD]`，当天的改动都累积在这一节里
   （同一天既有新增又有修复时，用 `### 新增` / `### 变更` / `### 修复` / `### 安全` 分子节）。
4. **不确定归哪类**：影响用户可感知行为 → 变更；修坏了的东西 → 修复；全新的东西 → 新增；
   涉及凭据 / 权限 / 泄露 → 安全。
5. **辅助生成**：`git log --format="%ad | %s" --date=short` 可以列出日期与提交标题，
   拿来对照着补条目，比翻 GitHub 快。
