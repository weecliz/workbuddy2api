# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机 / 局域网可直接使用的 **OpenAI / Anthropic 兼容 API**，并提供一个 **多账号代理共享平台**（账号池自动切换、独立 API Key、按 Key 配额、用量记账）。

`workbuddy2api` 不负责登录、不模拟桌面端、不替你执行工具。它只做三件事：

1. 读取本机登录态并注入完整的鉴权头（含设备风控头 `X-Device-Token`）
2. 在 OpenAI / Anthropic 协议与腾讯后端协议之间转换
3. 对 Codex CLI 这类长上下文 agent 请求做后端友好的压缩投影

---

## 项目运行截图
<img src="./images/img_1.png">
<img src="./images/img_2.png">
<img src="./images/img_3.png">

## 目录

- [一、逆向工程：解包 WorkBuddy 桌面端源码（app_source）](#一逆向工程解包-workbuddy-桌面端源码app_source)
- [二、逆向反代核心（workbuddy2api 网关）](#二逆向反代核心workbuddy2api-网关)
- [三、多账号代理共享平台（admin）](#三多账号代理共享平台admin)
- [四、环境安装与项目运行](#四环境安装与项目运行)
- [五、每日签到定时任务（daily_checkin）](#五每日签到定时任务daily_checkin)
- [六、客户端接入](#六客户端接入)
- [七、日志与排障](#七日志与排障)
- [八、项目结构](#八项目结构)
- [九、免责声明与协议](#九免责声明与协议)

---

## 一、逆向工程：解包 WorkBuddy 桌面端源码（app_source）

本项目在落地反代逻辑、补齐风控头之前，先对 **WorkBuddy 桌面端** 做了逆向分析，目的是拿到「真实接口形态 / 必需请求头 / 活动结束时间等字段」，而不是盲猜。产物是 `app_source/`（解包后的前端 + 主进程源码）。

> `app_source/` 是 **逆向产物，不在本仓库内**（存在于 `D:\workbuddy\app_source`），本仓库只收录「解包流程」与「反代实现」。

### 1.1 目标与边界

| 项 | 说明 |
|------|------|
| 安装目录 | `D:\workbuddy`（Windows，Git Bash 风格） |
| 主程序包 | `D:\workbuddy\resources\app.asar`（Electron 打包，约 287MB） |
| 解包产物 | `D:\workbuddy\app_source`（cli / main / preload / renderer） |
| 原生模块 | 桌面端安装目录下的 `resources/app.asar.unpacked/native/turing-sdk`（运行时由 `turing_helper.js` 自动发现本机安装位置，可用 `WORKBUDDY_TURING_SDK_DIR` 覆盖） |

解包不是为了修改桌面端，而是为了 **确认接口契约**：

- 每日签到：`POST /v2/billing/meter/checkin-activity-status`、`POST /v2/billing/meter/daily-checkin`
- 活动结束时间：`checkin-activity-status` 响应里的 `data.end_time`（即「下次停止领取」的依据）
- 设备风控头：`X-Device-Token`，由桌面端 Turing Shield SDK 生成，签到 / 对话等敏感请求都带
- 业务码：`1001=今日已领`、`1002=无资格`、`1003=活动已结束`

### 1.2 解包步骤

> 前置：本机已装 **Node.js**（含 npm）。as工具用 `asar` npm 包。

**（1）安装 asar 工具**（在 WorkBuddy 的 managed node workspace 里装，避免污染全局）：

```bash
cd "C:/Users/Administrator/.workbuddy/binaries/node/workspace"
npm install asar --no-save
```

**（2）全量解包会失败** —— `asar extract` 会去读 `app.asar.unpacked` 里缺失的二进制（如 `node-pty-win32-arm64\...\conpty\OpenConsole.exe`、`ripgrep/arm64-darwin/rg`），报 `ENOENT`。

**（3）改用「按需提取脚本」** `extract_source_files.js`（同 workspace 内），只抽 `main / preload / renderer` 的 `js / cjs / mjs / html / json`，避开 unpacked 原生二进制：

```js
const asar = require('asar');
const src  = 'D:\\workbuddy\\resources\\app.asar';
const dest = 'D:\\workbuddy\\app_source';
const prefixes = ['main', 'preload', 'renderer'];
const extensions = ['.js', '.cjs', '.mjs', '.html', '.json'];

const files = asar.listPackage(src)
  .map(f => f.startsWith('\\') ? f.slice(1) : f)
  .filter(f => prefixes.includes(f.split('\\')[0])
            && extensions.some(ext => f.endsWith(ext)));

for (const file of files) {
  const out = require('path').join(dest, file);
  require('fs').mkdirSync(require('path').dirname(out), { recursive: true });
  require('fs').writeFileSync(out, asar.extractFile(src, file));
}
```

运行：

```bash
node extract_source_files.js
```

产物结构：

```text
D:\workbuddy\app_source\
├── cli/          # product.json（含 turingSdk.channelId、版本号等配置）
├── main/         # Electron 主进程：AuthService、server.js、tar.js、index.js（Turing SDK 桥接）
├── preload/      # 预加载脚本（renderer ↔ main IPC 通道）
└── renderer/     # 前端打包代码（assets/*.js、国际化 zh-cn-*.js）
```

### 1.3 关键逆向发现（直接驱动了反代实现）

| 发现 | 位置 | 对反代的意义 |
|------|------|------|
| 设备风控头 `X-Device-Token` | `main/tar.js` `buildHeadersWithTuringToken` / `TURING_SHIELD_ID_HEADER="X-Device-Token"` | 反代必须给签到 / 对话请求注入该头，否则上游风控识别为「非真实客户端」 |
| Turing SDK 桥接 | `resources/app.asar.unpacked/native/turing-sdk/index.cjs`（`configure` + `fetchDeviceToken`） | 复用了同一 SDK 给 Python 网关取 token（见 [2.3](#23-设备风控头提供器)） |
| channelId = `109144` | `app_source/cli/product.json` → `turingSdk.channelId` | `turing_helper.js` 默认 channelId |
| 签到链路 | `main/tar.js` `claimDailyCheckin` → `POST /v2/billing/meter/daily-checkin` | 定时任务直接打该端点（见 [五](#五每日签到定时任务daily_checkin)） |
| RPC 通道 | `main/contract.js` `AUTH_RPC_CHANNELS`：`auth:getCheckinStatus` / `auth:claimDailyCheckin` | 仅桌面端内部用，反代走后端 HTTP 直连，不依赖 IPC |

---

## 二、逆向反代核心（workbuddy2api 网关）

### 2.1 架构

```text
客户端 (OpenAI/Anthropic SDK)
        │  /v1/chat/completions | /v1/responses | /v1/messages
        ▼
converter.py  (FastAPI)
        │  ├─ 注入鉴权头（Authorization / X-User-Id / X-Enterprise-Id / X-Tenant-Id / X-Domain / X-Device-Token）
        │  └─ 协议适配（OpenAI Chat ↔ Responses ↔ Anthropic Messages ↔ 腾讯 /v2/chat/completions）
        ▼
腾讯后端  https://copilot.tencent.com/v2/chat/completions
```

后端 `copilot.tencent.com` 本身走标准 OpenAI `chat/completions` 协议（含原生 `tools` / `tool_calls` / SSE 流式），转换器只在本地 `/v1/*` 与后端 `/v2/*` 之间做路径映射与透传。token 临近过期时自动调 `/v2/plugin/auth/token/refresh` 刷新并回写 `.info` 登录文件。

### 2.2 支持的端点

| 端点 | 说明 | 状态 |
|------|------|------|
| `POST /v1/chat/completions` | OpenAI Chat（流式） | 已支持 |
| `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） | 已支持 |
| `POST /v1/messages` | Anthropic Messages（适配 Claude Code / CC Switch） | 已支持 |
| `GET /v1/models` | 实时拉取后端模型，失败回退内置列表 | 已支持 |
| `GET /v1/balance` | 当前账号积分额度 | 已支持 |
| `GET /health` | 健康检查（含余额摘要） | 已支持 |

### 2.3 设备风控头提供器

反代的全部后端请求（含签到、对话）都会注入 `X-Device-Token`，来源是复用桌面端的 **Turing Shield SDK 原生模块**：

- `turing_helper.js`（项目根，Node）：`require()` 桌面端 `app.asar.unpacked/native/turing-sdk`，`configure(channelId, productName, productVersion)` 后 `fetchDeviceToken()`，向 stdout 输出 `{"token":"v3:..."}`。
- `admin/turing_token.py`（Python）：`subprocess` 调 `turing_helper.js`，进程内缓存 10 分钟，失败返回 `None`（调用方优雅降级，**不影响主流程**）。

可通过环境变量覆盖路径 / channelId：

```bash
WORKBUDDY_TURING_SDK_DIR       # SDK 目录（自动发现本机 WorkBuddy 安装位置；若安装目录特殊可显式指定以覆盖自动发现）
WORKBUDDY_TURING_CHANNEL_ID    # 默认 109144
WORKBUDDY_PRODUCT_NAME         # 默认 WorkBuddy
WORKBUDDY_VERSION              # 默认 2.0.0
```

> 若本机没装桌面端或 SDK 不可用，`get_headers()` 自动降级为不带 `X-Device-Token`，功能仍可跑，但敏感请求更易被风控识别。

### 2.4 三个协议适配器

- `responses_adapter.py` —— OpenAI Responses ↔ Chat 适配
- `anthropic_adapter.py` —— Anthropic Messages ↔ Chat 适配
- `responses_projection.py` —— Codex / agent 请求投影压缩（投影前后消息数 / 字符数 / tool schema 压缩量）
- `desensitize.py` —— 运行时文本压缩与零宽脱敏（去安全风险词，降低腾讯审核拦截率）

---

## 三、多账号代理共享平台（admin）

一个 **sub2 风格的反代理管理大屏**：把多个 WorkBuddy 账号集中管理，在还有额度的账号之间自动切换，并给不同用户发独立 API Key、按 Key 限额，超额直接拒绝。

### 3.1 能力

- **批量上传账号**：把桌面端 `.info` 登录文件原文（或数组 / 逐行）批量导入，存进 MySQL
- **账号池自动切换**：每次请求从「启用 + 还有剩余额度」的账号里挑选（默认剩余最多优先，可切 LRU）
- **查余额 / 刷新**：后台随时看每个账号总积分、剩余额度，并触发实时刷新
- **API Key 管理**：后台创建 Key 给别人用，可设每个 Key 的积分上限
- **配额拦截**：Key 已用积分 ≥ 上限时，代理直接返回 `402 {"error":{"message":"积分已耗尽","type":"quota_exceeded"}}`
- **用量记录**：每次调用落 `usage_logs`，可按 Key / 账号追溯
- **每日签到定时任务**：见 [五](#五每日签到定时任务daily_checkin)

### 3.2 稳定性设计（借鉴 `workbuddy2ap-2`）

| 机制 | 实现位置 | 说明 |
|------|----------|------|
| **连接池** | `converter.py` / `admin/backend.py` | `httpx.Limits(max_connections=100, max_keepalive_connections=20)`，减少 TLS 握手 |
| **账号级重试** | `admin/routers/proxy.py` | 单请求最多 3 次账号轮换；429/5xx/网络错误自动换号，401 session 死亡直接禁用 |
| **错误分类** | `_classify_error` | 余额不足 / 429 / 404 / 5xx / session 死亡 / 网络层 分别处理 |
| **errCount 策略** | `_apply_account_policy` | 网络层错误不累计；404 短冷却不累计；HTTP 5xx 累计，阈值 5 触发 10m 冷却 |
| **防撞号** | `_select_account` | `last_picked_at` 100ms 窗口，同一账号高并发时不被重复选中 |
| **状态持久化** | `accounts` 表 + `init_db` 迁移 | 冷却/错误计数/禁用原因直接落库，进程重启不丢失（DB 等价于 state.json） |
| **凭证续期** | `converter.CredentialManager._refresh` | token 临近过期自动刷新，刷新失败在代理层禁用账号 |
| **请求级表格日志** | `_log_chat_row` | 每个 `/v1/chat/completions` 请求出口打印 `seq / TTFB / uid / tokens / latency / error_kind` |

### 3.3 技术栈

- 后端：**FastAPI + SQLAlchemy 2.0 + MySQL 8（pymysql）+ Redis**
- 前端：**纯 HTML + TailwindCSS + FontAwesome**（CDN，无需构建），单页管理后台
- 鉴权：后台 JWT（HS256）；代理 API Key 用 SHA-256 存储，明文仅创建时展示一次

### 3.4 路由总览

| 模块 | 接口 | 说明 |
|------|------|------|
| 登录 | `POST /api/login` | 返回 JWT（放 `X-Admin-Token`） |
| 账号 | `GET/POST /api/accounts` · `POST /api/accounts/batch` | 账号列表 + 汇总 / 新增单个 / 批量导入 |
| 账号 | `POST /api/accounts/{id}/refresh` · `PATCH/DELETE /api/accounts/{id}` | 刷新余额 / 改状态 / 删除 |
| 账号 | `GET /api/accounts/scan-local` · `POST /api/accounts/import-local` · `POST /api/accounts/{id}/inject` | 扫描本机登录态 / 导入号池 / 注入回本机 |
| 账号 | `POST /api/oauth/start` · `GET /api/oauth/status/{login_id}` · `POST /api/oauth/commit/{login_id}` | **OAuth 一键加号**（不需要桌面端，见 [3.6](#36-oauth-一键加号不需要桌面端)） |
| Key | `GET/POST /api/keys` · `PATCH/DELETE /api/keys/{id}` | Key 列表（脱敏）/ 创建 / 改限额 / 停用 / 吊销 |
| 任务 | `GET/POST /api/schedules` · `PATCH/DELETE /api/schedules/{id}` · `POST /api/schedules/{id}/run` | 定时任务 CRUD / 立即运行 |
| 用量 | `GET /api/usage` · `GET /api/logs/usage` | 用量汇总 / 明细 |
| 代理网关 | `POST /v1/chat/completions` · `GET /v1/models` | 带 Key 校验 + 配额 + 记账 |
| 代理网关 | `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） |
| 代理网关 | `POST /v1/messages` · `POST /v1/messages/count_tokens` | Anthropic Messages（适配 Claude Code / CC Switch） |
| 后台页 | `GET /admin` | 管理大屏静态页 |

> 上面三条 `/v1/*` 网关路由**共用同一批 Key、同一套配额与用量记账**，账号都从号池自动挑选；
> 只是入口协议不同（Chat / Responses / Anthropic）。注意 `base_url` 的约定不一样：
> OpenAI 系客户端填 `http://<host>:8790/v1`，Anthropic 系（Claude Code）填 `http://<host>:8790`——
> 两种 SDK 都会自己拼后面的路径。

### 3.4 环境变量（admin）

`ADMIN_DATABASE_URL` · `ADMIN_REDIS_URL` · `ADMIN_BACKEND` · `ADMIN_USERNAME` · `ADMIN_PASSWORD` · `ADMIN_JWT_SECRET`（≥32 字节）· `ADMIN_JWT_EXPIRE_HOURS` · `ADMIN_COST_PER_TOKEN` · `ADMIN_ACCOUNT_SELECT`（`remain` / `lru`）· `ADMIN_PORT` · `ADMIN_CLIENT_AUTH_DIR`

OAuth 一键加号相关（`ADMIN_OAUTH_*`，均可省略）见 [3.6](#36-oauth-一键加号不需要桌面端)。
客户端身份画像相关（`ADMIN_UPSTREAM_CLIENT_KIND` / `ADMIN_UA_*`）见 [3.7](#37-客户端身份画像workbuddy--codebuddy)。

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

### 3.5 已知限制

- 账号凭据（`.info` 原文）以明文存于 MySQL，生产环境请加密存储或限制库访问
- 后端未回传 `credits` 时，按 `completion_tokens × COST_PER_TOKEN` 估算扣费（经验值）
- 配额扣减在流式结束后的 `finally` 里提交，高并发下非严格原子（极端竞态可能短暂超额）
- 余额刷新受腾讯后端限流影响（约每日 15:12 UTC+8 重置窗口），刷新失败余额保持不变

### 3.6 OAuth 一键加号（不需要桌面端）

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

### 3.7 客户端身份画像（workbuddy / codebuddy）

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

---

## 四、环境安装与项目运行

### 4.1 前置依赖

| 依赖 | 用途 | 版本 |
|------|------|------|
| Python | 运行 converter / admin | 3.10+（推荐 3.12） |
| Node.js | 设备风控头 `turing_helper.js`（require 桌面端 SDK） | 任意 LTS |
| MySQL | admin 账号池 / 用量库 | 8.x，默认 `root/root`，库名 `workbuddy_admin` |
| Redis | admin Key / 配额缓存 | 默认 6379 |
| WorkBuddy 桌面端 | 提供登录态 `.info` 与 Turing SDK | 已登录 |

> 本项目自带 managed 隔离环境：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（已装全部依赖）。`main.py` 启动时会自动检测到缺包并切换过去。

### 4.2 安装依赖

```bash
# 用仓库自带 venv（推荐）
"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -m pip install -r requirements.txt

# 或自建虚拟环境
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt`：`fastapi` · `uvicorn[standard]` · `httpx` · `sqlalchemy>=2.0` · `pymysql` · `redis` · `python-multipart` · `PyJWT` · `cryptography`

### 4.3 运行方式（三种）

#### 方式 A：单端口一体化（推荐生产 / 共享）

`python main.py` 一个进程同时拉起：管理后台 + 托管网关 + 内嵌 converter，全部走 **8790** 单端口：

```bash
# 前台常驻
python main.py
# 或指定监听
python main.py --host 0.0.0.0 --port 8790
```

启动后：

- 管理后台：`http://127.0.0.1:8790/admin`
- 托管网关（带 Key 配额）：`http://127.0.0.1:8790/v1/chat/completions`
- 内嵌网关（桌面登录态 / responses / messages）：`http://127.0.0.1:8790/gw/v1/...`

`Ctrl+C` 优雅关闭；子进程异常退出则整体退出（避免孤儿进程）。启动时会告警弱密钥 / 弱口令，部署请覆盖 `ADMIN_JWT_SECRET` / `ADMIN_PASSWORD`。

一键脚本（本机已配好）：`start_admin.bat`（强密码 + 固定 JWT secret 的一键启动）。

#### 方式 B：仅本机桌面端直连（converter 独立）

适合个人使用，直接吃桌面端实时登录态，额外支持 `/v1/responses`、`/v1/messages`、`/v1/balance`：

```bash
python converter.py --desensitize --log converter.log          # 默认 127.0.0.1:8787
python converter.py --port 9000 --api-key mysecret             # 自定义端口 / 本地鉴权
```

一键脚本：`start_converter.bat`。

#### 方式 C：仅管理后台（admin 独立）

```bash
python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790
```

### 4.4 同步登录态到服务器

`scripts/` 之外，根目录 `sync_auth.py` + `sync_auth.bat`：把本机最新桌面端登录态同步到服务器（依赖 managed python 的 paramiko），双击 `sync_auth.bat` 即可。

### 4.5 Docker

容器拿不到桌面端 auth 文件，需把宿主机登录态目录挂进去。改 `deploy/docker-compose.yml` 里的 auth 挂载路径后，在 `deploy/` 目录执行：

```bash
docker compose up -d --build
```

或单容器（context 是仓库根目录）：

```bash
docker build -f deploy/Dockerfile -t workbuddy2api .
docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

相关环境变量：`CODEBUDDY_AUTH_DIR` · `CODEBUDDY2OPENAI_KEY` · `CODEBUDDY2OPENAI_LOG`。

### 4.6 converter 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize`，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |

---

## 五、每日签到定时任务（daily_checkin）

基于 [一](#一逆向工程解包-workbuddy-桌面端源码app_source) 的逆向结论实现：自动给所有活跃账号领「每日 100 积分」，并自带 **风控保护**。

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

## 六、客户端接入

### Codex CLI（走 `/v1/responses`）

```toml
# ~/.codex/config.toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8790/v1"   # 或 8787（独立 converter）
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "你的任务描述"
```

### Claude Code / CC Switch（走 `/v1/messages`）

两条路，按「要不要多账号轮换 + Key 配额」来选：

**A. 走共享平台（`8790`）—— 带 Key 校验、配额、用量记账，账号从号池自动挑选**

```json
{
  "workbuddy-admin": {
    "base_url": "http://127.0.0.1:8790",
    "api_key": "后台 API Keys 页创建的那把 sk-...",
    "model": "claude-sonnet-4-5-20250929"
  }
}
```

- **`base_url` 不要带 `/v1`**：Anthropic SDK 会自己拼 `/v1/messages`，填成 `.../v1` 会变成 `/v1/v1/messages`。（对比：OpenAI 系客户端要填 `.../v1`。）
- 模型名可以照抄 Claude 官方的 `claude-sonnet-4-5-*` 这类名字 —— 服务端会按 opus / sonnet / haiku 三档自动映射到白名单里的模型；也可以直接填 `glm-5.2` 这类真实模型名。
- **harness 脱敏默认开启**，无需额外参数。这一步不能省：Claude Code 的 system prompt 里有
  "DoS attacks / exploit development / credential testing" 这类**拒绝作恶的合规声明**，
  不做脱敏会被后端内容审核当成敏感内容整条拒绝，报错是极具误导性的
  `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`。
  ⚠️ 排查提示：不脱敏时简单的 `"hello"` 请求**能通过**，只有真实 Claude Code 的完整 harness 才会被拦，
  所以**不要用 hello 请求验证这个端点**。

**B. 走本机直连（`8787`，`python converter.py`）—— 只用自己的桌面端登录态，无配额**

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

- 模型名必须填腾讯后端真实模型名；不做自动映射
- 强烈建议开启 `--desensitize`

### 其它 OpenAI 兼容客户端（Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI）

- Base URL：`http://127.0.0.1:8790/v1`（共享平台）或 `:8787/v1`（本机直连）
- API Key：留空，或填启动时 `--api-key` / 后台创建的 `wb-...` Key
- 模型名：`glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

```bash
curl -N http://127.0.0.1:8790/v1/chat/completions \
  -H "X-API-Key: 你的KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

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

## 八、项目结构

```text
workbuddy2api/
├── main.py                   # 一键单端口启动：管理后台 + 托管网关 + 内嵌网关
├── service_admin.py          # Windows 服务宿主（与 main.py 跑同一个 admin.server:app）
├── turing_helper.js          # Node：调用桌面端 Turing Shield SDK 取设备风控 token
├── requirements.txt          # Python 依赖
├── core/                     # 内核：上游协议适配与凭据管理
│   ├── converter.py          # CredentialManager + /gw 单账号旁路 FastAPI app
│   ├── responses_adapter.py  # OpenAI Responses ↔ Chat 适配
│   ├── responses_projection.py # Codex / agent 请求投影压缩
│   ├── anthropic_adapter.py  # Anthropic Messages ↔ Chat 适配
│   └── desensitize.py        # 运行时文本压缩与零宽脱敏
├── admin/                    # 多账号管理后台（FastAPI + MySQL + Redis）
│   ├── server.py             # FastAPI 入口、登录、静态页挂载、converter 挂 /gw
│   ├── config.py             # 配置（环境变量覆盖）
│   ├── db.py                 # SQLAlchemy 引擎 / 会话 / 建库建表 / 列迁移
│   ├── models.py             # Account / ApiKey / UsageLog / Schedule ORM
│   ├── security.py           # JWT、Key 哈希、配额拦截
│   ├── backend/              # 单账号上游会话（按域拆分）
│   │   ├── session.py        # AccountSession：凭据落盘/回写 + 档案与额度
│   │   ├── checkin.py        # 每日签到
│   │   ├── growth.py         # 猫猫领养 / 旅行 / 连登 / 活跃上报
│   │   └── http.py           # 连接池参数、凭据元信息解析
│   ├── tasks/                # 后台任务实现（一个任务一个文件）
│   │   └── daily_checkin.py / cat_travel.py / activity_report.py / common.py
│   ├── scheduler.py          # 调度框架：轮询 schedules 表并分发到 admin/tasks/
│   ├── turing_token.py       # Python 侧 X-Device-Token 提供器（subprocess 调 helper）
│   ├── oauth_login.py        # OAuth 设备授权登录（浏览器登录换凭据，不需桌面端）
│   ├── routers/              # accounts / oauth / keys / proxy / schedules / logs / sync / models
│   └── static/index.html     # 纯 HTML + TailwindCSS + FontAwesome 管理大屏
├── deploy/                   # Docker 部署配置（build context 是仓库根目录）
│   ├── Dockerfile            # converter 独立版（8787，需挂载桌面端 auth）
│   ├── Dockerfile.admin      # admin 独立版（8790，Sealos / 容器平台）
│   └── docker-compose.yml
├── docs/                     # 部署文档：DEPLOY_WINDOWS / DEPLOY_SEALOS / ENV_SETUP
├── examples/                 # 客户端接入示例（手动合并进自己的配置，脚本不自动改写）
│   └── codex-codebuddy.example.toml  # Codex CLI / Claude Code / 其它客户端接入片段
├── scripts/                  # 本机一键脚本：start_admin / start_converter / 服务安装卸载
├── tests/                    # pytest：代理重试骨架 + 猫猫旅行状态机
└── README.md

# 逆向产物（不在本仓库，存在于 D:\workbuddy）
D:\workbuddy\app_source\      # cli / main / preload / renderer 解包源码
D:\workbuddy\resources\app.asar.unpacked\native\turing-sdk\   # 设备风控原生模块（运行时不写死此路径，由 turing_helper.js 自动发现）
```

---

## 九、免责声明与协议

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

协议：[MIT](./LICENSE)

---

## 十、更新日志

每次功能 / 修复 / 重构的变更记录见 [CHANGELOG.md](./CHANGELOG.md)。

> 致谢：本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。
