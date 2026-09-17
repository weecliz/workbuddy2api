# 管理平台（admin）架构与配置细节

> 本文是 README 的细节拆分：admin 平台的能力明细、稳定性设计、路由总览、环境变量、
> OAuth 加号全流程、客户端身份画像、设备指纹三头、Codex custom 工具的协议处理。

## 三、多账号代理共享平台（admin）

一个 **sub2 风格的反代理管理大屏**：把多个 WorkBuddy 账号集中管理，在还有额度的账号之间自动切换，并给不同用户发独立 API Key、按 Key 限额，超额直接拒绝。

### 3.1 能力

- **批量上传账号**：把桌面端 `.info` 登录文件原文（或数组 / 逐行）批量导入，存进数据库
- **账号池自动切换**：每次请求从「启用 + 还有剩余额度」的账号里挑选（默认剩余最多优先，可切 LRU）
- **查余额 / 刷新**：后台随时看每个账号总积分、剩余额度，并触发实时刷新
- **API Key 管理**：后台创建 Key 给别人用，可设每个 Key 的积分上限
- **配额拦截**：Key 已用积分 ≥ 上限时，代理直接返回 `402 {"error":{"message":"积分已耗尽","type":"quota_exceeded"}}`
- **用量记录**：每次调用落 `usage_logs`，可按 Key / 账号追溯
- **用量统计页**：直读**上游真实账单**（`/billing/meter/get-user-request-usage`），
  支持任意起止日期的汇总（按天 / 按模型 / 分账号），并带「前一天 / 后一天」
  按区间跨度整体平移；缺省当天，另带 今天 / 昨天 / 近 7 天 / 近 30 天 / 本月 快捷区间。
  日期选择有限制（不可选未来、起止联动、跨度上限 366 天）。
  与「日志」页的区别：日志是网关自己的记账（逐条可查），统计页是上游权威口径
  （含桌面端直连等不经网关的调用）
- **每日签到定时任务**：见 [docs/TASKS.md](./TASKS.md)

### 3.2 稳定性设计（借鉴 `workbuddy2ap-2`）

| 机制 | 实现位置 | 说明 |
| ------ | ---------- | ------ |
| **连接池** | `converter.py` / `admin/backend.py` | `httpx.Limits(max_connections=100, max_keepalive_connections=20)`，减少 TLS 握手 |
| **账号级重试** | `admin/routers/proxy.py` | 单请求最多 3 次账号轮换；429/5xx/网络错误自动换号，401 session 死亡直接禁用 |
| **错误分类** | `_classify_error` | 余额不足 / 429 / 404 / 5xx / session 死亡 / 网络层 分别处理 |
| **errCount 策略** | `_apply_account_policy` | 网络层错误不累计；404 短冷却不累计；HTTP 5xx 累计，阈值 5 触发 10m 冷却 |
| **防撞号** | `_select_account` | `last_picked_at` 100ms 窗口，同一账号高并发时不被重复选中 |
| **状态持久化** | `accounts` 表 + `init_db` 迁移 | 冷却/错误计数/禁用原因直接落库，进程重启不丢失（DB 等价于 state.json） |
| **凭证续期** | `converter.CredentialManager._refresh` | token 临近过期自动刷新，刷新失败在代理层禁用账号 |
| **请求级表格日志** | `_log_chat_row` | 每个 `/v1/chat/completions` 请求出口打印 `seq / TTFB / uid / tokens / latency / error_kind` |

### 3.3 技术栈

- 后端：**FastAPI + SQLAlchemy 2.0 + Redis**，数据库默认 **SQLite（零依赖单文件，开箱即跑）**，可切 **MySQL 8（pymysql）** / **IBM Db2（ibm_db_sa）**，由 `ADMIN_DB_TYPE` 一处切换（见 [docs/DB_SUPPORT.md](docs/DB_SUPPORT.md)）
- 前端：**纯 HTML + TailwindCSS + FontAwesome**（依赖随仓库放在 `admin/static/vendor/`，无需构建、无需外网），单页管理后台
- 鉴权：后台 JWT（HS256）；代理 API Key 用 SHA-256 存储，明文仅创建时展示一次

### 3.4 路由总览

| 模块 | 接口 | 说明 |
| ------ | ------ | ------ |
| 登录 | `POST /api/login` | 返回 JWT（放 `X-Admin-Token`） |
| 账号 | `GET/POST /api/accounts` · `POST /api/accounts/batch` | 账号列表 + 汇总 / 新增单个 / 批量导入 |
| 账号 | `POST /api/accounts/{id}/refresh` · `PATCH/DELETE /api/accounts/{id}` | 刷新余额 / 改状态 / 删除 |
| 账号 | `GET /api/accounts/scan-local` · `POST /api/accounts/import-local` · `POST /api/accounts/{id}/inject` | 扫描本机登录态 / 导入号池 / 注入回本机 |
| 账号 | `POST /api/oauth/start` · `GET /api/oauth/status/{login_id}` · `POST /api/oauth/commit/{login_id}` | **OAuth 一键加号**（不需要桌面端，见 [3.7](#37-oauth-一键加号不需要桌面端)） |
| Key | `GET/POST /api/keys` · `PATCH/DELETE /api/keys/{id}` | Key 列表（脱敏）/ 创建 / 改限额 / 停用 / 吊销 |
| 任务 | `GET/POST /api/schedules` · `PATCH/DELETE /api/schedules/{id}` · `POST /api/schedules/{id}/run` | 定时任务 CRUD / 立即运行 |
| 用量 | `GET /api/usage/summary` · `GET /api/usage/accounts` | **上游真实用量**：区间汇总（按天/按模型/按账号）/ 可选账号列表；支持任意起止日期，非仅当日 |
| 用量 | `GET /api/usage/detail` | 请求明细（分页，可按模型过滤）。**页面已不再使用**（无 UI 入口），接口保留供脚本/排查调用 |
| 用量 | `GET /api/logs` · `GET /api/logs/export` | 本地记账明细（网关自己的 `usage_logs` 表）/ 导出 CSV |
| 用量 | `POST /api/usage/cache/clear` | 清空用量短缓存（排查数据不一致时用） |
| 代理网关 | `POST /v1/chat/completions` · `GET /v1/models` | 带 Key 校验 + 配额 + 记账 |
| 代理网关 | `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） |
| 代理网关 | `POST /v1/messages` · `POST /v1/messages/count_tokens` | Anthropic Messages（适配 Claude Code / CC Switch） |
| 后台页 | `GET /admin` | 管理大屏静态页 |

> 上面三条 `/v1/*` 网关路由**共用同一批 Key、同一套配额与用量记账**，账号都从号池自动挑选；
> 只是入口协议不同（Chat / Responses / Anthropic）。注意 `base_url` 的约定不一样：
> OpenAI 系客户端填 `http://<host>:8790/v1`，Anthropic 系（Claude Code）填 `http://<host>:8790`——
> 两种 SDK 都会自己拼后面的路径。

### 3.5 环境变量（admin）

`ADMIN_DB_TYPE`（`sqlite` 默认 / `mysql` / `db2`）· `ADMIN_DB_HOST` · `ADMIN_DB_PORT` · `ADMIN_DB_USER` · `ADMIN_DB_PASSWORD` · `ADMIN_DB_NAME`（SQLite 时是数据文件路径；`ADMIN_DB_SCHEMA` 用于 DB2）· `ADMIN_REDIS_URL` · `ADMIN_BACKEND` · `ADMIN_USERNAME` · `ADMIN_PASSWORD` · `ADMIN_JWT_SECRET`（≥32 字节）· `ADMIN_JWT_EXPIRE_HOURS` · `ADMIN_COST_PER_TOKEN` · `ADMIN_ACCOUNT_SELECT`（`remain` / `lru`）· `ADMIN_PORT` · `ADMIN_CLIENT_AUTH_DIR`

也可用旧的 `ADMIN_DATABASE_URL` 直接给连接串（优先级更高）。

**也可以在启动命令上直接指定数据库**（优先级高于 `.env`，只影响本次启动）：

```bash
python main.py                                              # 默认 SQLite，零依赖直接跑
python main.py --db-type mysql --db-password xxx            # 临时改用 MySQL
python main.py --db-type db2 --db-name WBADMIN --db-schema WBADMIN
python main.py --list-db-types                              # 列出支持的类型
```

数据库类型 / 参数 / 各库差异见 [docs/DB_SUPPORT.md](docs/DB_SUPPORT.md)。

OAuth 一键加号相关（`ADMIN_OAUTH_*`，均可省略）见 [3.7](#37-oauth-一键加号不需要桌面端)。
客户端身份画像相关（`ADMIN_UPSTREAM_CLIENT_KIND` / `ADMIN_UA_*`）见 [3.8](#38-客户端身份画像workbuddy--codebuddy)。

Anthropic 端点（`/v1/messages`）相关：

- `ADMIN_ANTHROPIC_MODEL_OPUS` / `ADMIN_ANTHROPIC_MODEL_SONNET` / `ADMIN_ANTHROPIC_MODEL_HAIKU` —— Claude 的模型名按这三个档次映射到白名单模型，默认 `deepseek-v4-pro` / `glm-5.2` / `glm-5.3-flash`。**不要设成 `auto`**：本后台的 `auto` 语义是「取第一个启用的模型」，在 20+ 个模型里可能挑到不适合写代码的，甚至图像模型。
- `ADMIN_ANTHROPIC_DESENSITIZE`（默认 `1`）—— harness 脱敏开关，见 §6 说明，**关掉基本发不出去**
- `ADMIN_ANTHROPIC_NO_COMPACT`（默认 `0`）—— 只做零宽脱敏、跳过 harness 压缩
- `ADMIN_OPENAI_DESENSITIZE`（默认 `0`）—— 把同一套 harness 脱敏也应用到
  `/v1/chat/completions` 与 `/v1/responses`。**用 OpenAI 协议接入长 harness 客户端
  （Pi、claude-code-router 等）时才需要开**；普通短 prompt 客户端开了只会无谓改动提示词

内嵌 `/gw` 网关相关：

- `CONVERTER_API_KEY` —— **必设**。`converter._check_auth()` 是 `if not key: return`，留空等于完全不鉴权，而服务默认监听 `0.0.0.0`
- `CODEBUDDY_AUTH_DIR` —— converter 读取桌面端凭据的目录。**注意与 `ADMIN_CLIENT_AUTH_DIR` 是两个不同的变量**：后者给后台「扫描本机 / 注入本机」用。以 Windows 服务（LocalSystem）方式运行时两者都必须写**绝对路径**，否则 `%LOCALAPPDATA%` 会解析到空目录
- `CONVERTER_DESENSITIZE` · `CONVERTER_LOG`

### 3.6 已知限制

- 账号凭据（`.info` 原文）以明文存于数据库，生产环境请加密存储或限制库访问
- 后端未回传 `credits` 时，按 `completion_tokens × COST_PER_TOKEN` 估算扣费（经验值）
- 配额扣减在流式结束后的 `finally` 里提交，高并发下非严格原子（极端竞态可能短暂超额）
- 余额刷新受腾讯后端限流影响（约每日 15:12 UTC+8 重置窗口），刷新失败余额保持不变
- 成长任务（growth_tasks）的专家类任务上报**去重计数**（上报 5 次只推进 3/5），
  单轮点不满，靠每日回读进度补缺口多轮收敛；详见 §5之二「实测已知行为」

### 3.7 OAuth 一键加号（不需要桌面端）

账号页的 **「OAuth 添加」** 按钮：在浏览器完成一次官方登录即可把账号加进号池，
**不需要桌面端参与、也不需要手工拷贝 `.info`**。云上部署（Sealos 等）时这条路径最省事。

流程与官方 CodeBuddy CLI / WorkBuddy 桌面端**完全同一套**（两端 `product.json` 的
`platform` 都是 `CLI`、`prefixPath` 都是 `/plugin`，只有 `productName` 与 `auth.id` 不同）：

```
① POST {backend}/v2/plugin/auth/state?platform=CLI   -> {state, authUrl}
② 人工在浏览器打开 authUrl 完成登录
③ GET  {backend}/v2/plugin/auth/token?state=<state>  -> {accessToken, refreshToken, expiresIn, domain}
④ GET  {backend}/v2/plugin/login/account?state=…     -> {uid, enterpriseId, nickname}   （带 Bearer）
```

无 PKCE、无 `client_secret`、无 device_code —— 全部用标准库 `httpx` 实现
（`admin/oauth_login.py`），不依赖官方客户端的任何二进制。

**怎么知道对方登录完了？—— 没有回调，只有轮询。**

上游不推送、也不存在回调地址，全靠主动问。三层都是「问」：

```
浏览器（前端）  setInterval 2.5s
   └─ GET /api/oauth/status/{login_id}
        后端 poll_login()                     ← 只有前端来问，后端才去问上游；
   └─ GET {backend}/v2/plugin/auth/token?state=<state>
        上游：state 还没绑账号 → 返回业务码 11217（"login ing"）
        …
        对方在手机完成登录 → 上游把账号绑到 state
        下一次轮询     → 返回 {accessToken, refreshToken, expiresIn, domain}
   └─ GET {backend}/v2/plugin/login/account?state=<state>   （拿 uid / 昵称）
```

好处是**不需要公网回调地址、不用额外端口、不用验签**，Sealos 上少一堆配置；
代价是必须有人一直在问 —— 所以前端轮询是这条链路的心跳。

**业务码语义（照搬官方实现，别自行放宽）**

| 端点 | pending 码 | 其他码 |
| --- | --- | --- |
| `/v2/plugin/auth/token?state=` | `11217` | **一律致命**，立即终止并回传前端 |
| `/v2/plugin/login/account?state=` | `12151` | 告警后放弃（凭据已到手，只缺昵称/uid） |

官方轮询参数：间隔 **1 秒**、总超时 **300 秒**。本项目间隔放宽到 2.5 秒（前端），会话 TTL 600 秒。

> ⚠️ **不要把「任何非 0 码」当成 pending**。官方只认上面这两个码，其余非 0 码会直接抛
> `Failed to fetch auth token`。若笼统当成 pending，真实错误会伪装成「一直在等待登录」，
> 直到超时才暴露 —— 这正是本项目第一版实现踩过的坑，已修。
> 判定成功的口径也要对齐官方：**只看 `data.accessToken` 是否为非空字符串**，不看 code。

> ℹ️ 实测发现：**上游对「伪造的 state」也返回 `11217`**，即 pending 与「state 不存在」
> 在 token 端点侧不可区分。本项目靠自己的会话 TTL 兜底 —— 过期即回收，前端会拿到 404 并提示重新发起。

> 🔗 **链接可以转发给他人**（手机浏览器同样可用）。`authUrl` 的 query 只有
> `platform` + `state`，**不含任何设备指纹**，所以授权天然跨设备、跨人。
> 语义是「**谁在链接里完成登录，就加谁的账号**」——因为 state 记录的是登录结果，不是发起方。
> 因此请勿把链接发到公开渠道（会被塞进无关账号）；反过来，拿到链接的人也**碰不到你的账号**。
> 实操建议：一个链接只发给一个人，10 分钟内完成。

**安全设计（务必了解）**

| 项 | 做法 |
| --- | --- |
| `state` 的存放 | **只在服务端内存**。`state` 本身就是凭据（谁拿到谁能 poll 出 token），所以绝不下发浏览器、也不写日志 |
| 前端句柄 | 只给一次性随机 `login_id`（32 字符 `token_urlsafe`），真实 `state` 不出后端 |
| 鉴权 | 三个接口（`start` / `status` / `commit`）全部要求管理员登录态（`X-Admin-Token`） |
| 有效期 | 会话默认 **10 分钟**过期，过期即回收 `state` 与 HTTP 客户端 |
| 一次性 | `commit` 领取凭据后**立即销毁会话**，token 不长期驻留内存 |
| token 回传浏览器 | **不回**。`status` 只返回昵称 / uid / 域等元信息；凭据由服务端直接写入数据库 |
| 会话隔离 | 每个登录流程独立 `httpx.Client`（自带 cookie jar），多账号连续登录互不串会话 |

**重复授权**：若 `uid` 已在号池中，`commit` 走**更新**而不是新增 —— 覆盖 `auth_json`，
并把 `status` / `err_count` / `cool_until` 一并复位（token 失效后重新授权即可，不会堆重复条目）。

**相关环境变量**（都可省略，用默认值即可）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_OAUTH_PLATFORM` | `CLI` | 授权时带的 `platform`。官方 CLI 与桌面端**都是 `CLI`** |
| `ADMIN_OAUTH_USER_AGENT` | `CLI/2.148.0 CodeBuddy/2.148.0` | 出站 UA。官方格式为 `{platform}/{ver} {productName}/{ver}`，**随客户端版本变化，建议按本机实际版本调整**；走桌面端身份可改为 `CLI/<ver> WorkBuddy/<ver>` |
| `ADMIN_OAUTH_ORIGIN` | `https://www.codebuddy.cn` | `Origin` / `Referer` |
| `ADMIN_OAUTH_TTL` | `600` | 登录会话存活秒数 |
| `ADMIN_OAUTH_TIMEOUT` | `15` | 单次上游请求超时秒数 |

**前端行为**：点按钮 → 弹窗给出授权链接（可一键打开 / 复制）→ 前端每 2.5 秒轮询一次状态
→ 登录完成自动跳到确认页（显示昵称 / UID / 域）→ 点「加入号池」入库并刷新余额。
关闭弹窗或按 Esc 会清掉轮询定时器（统一挂在 `_modalCleanups` 上）。

> 排查用：`GET /api/oauth/pending` 返回当前未完成的登录会话数，可确认没有悬挂的 `state`。

### 3.8 客户端身份画像（workbuddy / codebuddy）

官方两个客户端 —— **WorkBuddy 桌面端** 与 **CodeBuddy CLI** —— 在服务端看来**是同一套客户端**：

| product.json 字段 | WorkBuddy 桌面端 | CodeBuddy CLI |
| --- | --- | --- |
| `platform` | `CLI` | `CLI` |
| `deploymentType`（→ `X-Product`） | `SaaS` | `SaaS` |
| `authentication.attributes.prefixPath` | `/plugin` | `/plugin` |
| `authentication.id`（→ `.info` 文件名） | `workbuddy-desktop` | `Tencent-Cloud.coding-copilot` |
| `productName`（→ UA 里的产品名） | `WorkBuddy` | `CodeBuddy` |

两端端点完全相同、`X-Auth-Refresh-Source` 都是 `plugin`。**出站请求只有两处差异**：

| 头 | workbuddy | codebuddy |
| --- | --- | --- |
| `User-Agent` | `CLI/<版本> WorkBuddy/<版本>` | `CLI/<版本> CodeBuddy/<版本>` |
| `X-Device-Token` | 带（仅「模型 / 签到」请求，且本机装有 Turing SDK 时才取得到） | **不带** |

> 因此按域自动选择身份的意义是**让请求画像自洽**：既然是桌面端身份就发桌面端 UA，
> 既然是 CLI 身份就别带设备指纹。混搭（发桌面端 UA 却不带设备指纹、
> 或带设备指纹却发 CLI UA）比"完全不像"更容易被风控盯上。

**怎么判定**

- **无需任何配置与存储**：出站时按凭据自身的 `auth.domain` 现算——
  含 `workbuddy` → 桌面端身份，其它非空 → CLI 身份；domain 缺失时用
  `ADMIN_UPSTREAM_CLIENT_KIND` 兜底（默认 workbuddy）。
- 推断结果不落库：同一份凭据换域（企业切换等）后，下次出站自动跟随新域。

> ⚠️ **升级注意**：改动前所有账号的出站 UA 都是自造的 `codebuddy2openai/2.0`，
> 现在会变成真实的客户端 UA。这是有意的修正（那个自造名本身就是画像不自洽的风险点）。
> 想完全恢复旧行为：`ADMIN_UA_WORKBUDDY=codebuddy2openai/2.0`。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_UPSTREAM_CLIENT_KIND` | `workbuddy` | 凭据里 `auth.domain` 缺失时的兜底身份 |
| `ADMIN_UA_WORKBUDDY` | `CLI/5.3.14 WorkBuddy/5.3.14` | 桌面端 UA。**版本号是按官方拼装规则与其 product.json 推断的**（桌面端不打印 API 请求头，无法从日志确认），升级客户端后请同步 |
| `ADMIN_UA_CODECLI` | `CLI/2.148.0 CodeBuddy/2.148.0` | CLI UA。版本号来自本机真实请求日志，**实测确认** |
| `ADMIN_DEVICE_FINGERPRINT` | `on` | 是否注入稳定设备指纹三头（见 §3.9）。仅作回滚逃生口，设 `off` 恢复改动前行为 |

### 3.9 设备指纹头（`X-Machine-ID` / `X-Session-ID` / `X-Request-ID`）

上面 §3.8 讨论的是 `X-Device-Token`（需本机 Turing SDK）。这里三个头是**另一套机制**，
两者不要混：

| | 来源 | 依赖 | 拿不到时 |
| --- | --- | --- | --- |
| `X-Device-Token` | 桌面端 Turing Shield SDK | **必须本机装桌面端** | 不注入该头 |
| `X-Machine-ID` 等三头 | 账号 uid 纯哈希 | **无任何外部依赖** | —— |

- **算法**（`core/fingerprint.py`）：`md5("<salt>:<uid>")[:36]`，salt 为 `machine` / `session` / `req`。
  与参考实现 workbuddy2api-hub 的 `wb_fingerprint.py` **逐字对齐** —— 这一点是刻意的：
  换端或混用同一号池时，同一 uid 必须派生出**相同**设备标识，否则同一账号在两套程序下
  表现为两台不同设备，反而更容易被判定为异常登录。
- **注入点**：`core/converter.py` 的 `CredentialManager._build_headers_from()`，
  是对话 / 签到 / 猫猫旅行 / 活跃上报的**唯一共同出口**，一处改动全覆盖。
- **为什么不像 `X-Device-Token` 那样做成「跟随可用性」**：两者机制无关（一个是 SDK、
  一个是纯哈希），绑定只会让云端部署（拿不到 `X-Device-Token`）白白失去
  「同账号设备稳定 + 多账号彼此隔离」这项收益。
- **证据边界**：README §1.3 的逆向发现里**只有 `X-Device-Token`**，没有
  `X-Machine-ID` / `X-Session-ID` 被上游校验的证据。因此这项的准确定位是
  「与成熟项目行为一致的加固」，而非「已验证上游会校验这三个头」。
- **回滚**：`.env` 设 `ADMIN_DEVICE_FINGERPRINT=off`。

### 3.10 Codex 自由格式（custom）工具

Codex CLI 的 `apply_patch` 用 Responses 的 `type: "custom"` 声明：没有 `parameters`，
只有一个自由文本入参。上游 Chat 协议不认这个类型，需要三层协同处理
（`core/responses_adapter.py`，缺任一层则整项失效）：

1. **请求侧**：降级为「单个 `input` 字符串参数」的 function 工具，
   并在 description 里注入「原样输出完整载荷、勿包 JSON / 勿加代码块」的提示；
   `format.definition`（grammar）也一并附进 description 保留信息。
2. **投影侧**：`_project_tools()` 与 `SCHEMA_KEEP_KEYS` 必须保留 `description`
   —— 否则第 1 步注入的提示会被立刻剥掉，降级等于白做。
3. **响应侧**：调用还原为 `custom_tool_call` 项 + `response.custom_tool_call_input.delta/done`
   事件（**不发** `function_call_arguments.*`），并把 `{"input":"..."}` 拆回自由文本原文
   —— Codex 认这个形状。

> 未声明 custom 工具的请求（Chatbox / LobeChat 等普通 OpenAI 客户端）**行为完全不变**：
> `custom_names` 为空集时走原路径。

---

