# 更新日志

记录本项目的每次重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
项目目前**没有打版本标签**，所以条目用**日期**做标题（将来引入 tag 后可平行换成版本号）。

条目分类：

- **新增** Added：新功能、新端点、新脚本
- **变更** Changed：行为变化、重构、目录与配置调整
- **修复** Fixed：缺陷修复
- **安全** Security：凭据、权限、泄露相关

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
