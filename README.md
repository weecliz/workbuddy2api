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

Anthropic 端点（`/v1/messages`）相关：

- `ADMIN_ANTHROPIC_MODEL_OPUS` / `ADMIN_ANTHROPIC_MODEL_SONNET` / `ADMIN_ANTHROPIC_MODEL_HAIKU` —— Claude 的模型名按这三个档次映射到白名单模型，默认 `deepseek-v4-pro` / `glm-5.2` / `glm-5.3-flash`。**不要设成 `auto`**：本后台的 `auto` 语义是「取第一个启用的模型」，在 20+ 个模型里可能挑到不适合写代码的，甚至图像模型。
- `ADMIN_ANTHROPIC_DESENSITIZE`（默认 `1`）—— harness 脱敏开关，见 §6 说明，**关掉基本发不出去**
- `ADMIN_ANTHROPIC_NO_COMPACT`（默认 `0`）—— 只做零宽脱敏、跳过 harness 压缩

内嵌 `/gw` 网关相关：

- `CONVERTER_API_KEY` —— **必设**。`converter._check_auth()` 是 `if not key: return`，留空等于完全不鉴权，而服务默认监听 `0.0.0.0`
- `CODEBUDDY_AUTH_DIR` —— converter 读取桌面端凭据的目录。**注意与 `ADMIN_CLIENT_AUTH_DIR` 是两个不同的变量**：后者给后台「扫描本机 / 注入本机」用。以 Windows 服务（LocalSystem）方式运行时两者都必须写**绝对路径**，否则 `%LOCALAPPDATA%` 会解析到空目录
- `CONVERTER_DESENSITIZE` · `CONVERTER_LOG`

### 3.5 已知限制

- 账号凭据（`.info` 原文）以明文存于 MySQL，生产环境请加密存储或限制库访问
- 后端未回传 `credits` 时，按 `completion_tokens × COST_PER_TOKEN` 估算扣费（经验值）
- 配额扣减在流式结束后的 `finally` 里提交，高并发下非严格原子（极端竞态可能短暂超额）
- 余额刷新受腾讯后端限流影响（约每日 15:12 UTC+8 重置窗口），刷新失败余额保持不变

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

容器拿不到桌面端 auth 文件，需把宿主机登录态目录挂进去。改 `docker-compose.yml` 里的 auth 挂载路径后：

```bash
docker compose up -d --build
```

或单容器：

```bash
docker build -t workbuddy2api .
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
├── converter.py              # 内嵌网关主入口（FastAPI），挂载到 /gw/v1（main.py 下）
├── main.py                   # 一键单端口启动：管理后台 + 托管网关 + 内嵌网关
├── responses_adapter.py      # OpenAI Responses ↔ Chat 适配
├── responses_projection.py   # Codex / agent 请求投影压缩
├── anthropic_adapter.py      # Anthropic Messages ↔ Chat 适配
├── desensitize.py            # 运行时文本压缩与零宽脱敏
├── turing_helper.js          # Node：调用桌面端 Turing Shield SDK 取设备风控 token
├── sync_auth.py / .bat       # 同步本机登录态到服务器
├── start_converter.bat       # 本机直连网关一键启动
├── start_admin.bat           # 管理后台一键启动（强密码 + 固定 JWT secret）
├── requirements.txt / Dockerfile / docker-compose.yml
├── scripts/
│   └── test_daily_checkin.py # 签到验证脚本（仅对未领账号真实领取）
├── admin/                    # 多账号管理后台（FastAPI + MySQL + Redis）
│   ├── server.py             # FastAPI 入口、登录、静态页挂载、converter 挂 /gw
│   ├── config.py             # 配置（环境变量覆盖）
│   ├── db.py                 # SQLAlchemy 引擎 / 会话 / 建库建表 / 列迁移
│   ├── models.py             # Account / ApiKey / UsageLog / Schedule ORM
│   ├── security.py           # JWT、Key 哈希、配额拦截
│   ├── backend.py            # 复用 converter.CredentialManager 操作单账号（含签到）
│   ├── scheduler.py          # 轻量定时任务：refresh_balances / sync_models / daily_checkin
│   ├── turing_token.py       # Python 侧 X-Device-Token 提供器（subprocess 调 helper）
│   ├── routers/              # accounts / keys / proxy / schedules / logs / sync / models
│   └── static/index.html     # 纯 HTML + TailwindCSS + FontAwesome 管理大屏
└── README.md

# 逆向产物（不在本仓库，存在于 D:\workbuddy）
D:\workbuddy\app_source\      # cli / main / preload / renderer 解包源码
D:\workbuddy\resources\app.asar.unpacked\native\turing-sdk\   # 设备风控原生模块（运行时不写死此路径，由 turing_helper.js 自动发现）
```

---

## 九、免责声明与协议

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

协议：[MIT](./LICENSE)

> 致谢：本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。
