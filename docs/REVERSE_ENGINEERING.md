# 逆向工程与网关核心

> 本文是 README 的细节拆分：逆向分析过程、解包步骤、关键发现，以及反代核心的架构与端点清单。

## 一、逆向工程：解包 WorkBuddy 桌面端源码（app_source）

本项目在落地反代逻辑、补齐风控头之前，先对 **WorkBuddy 桌面端** 做了逆向分析，目的是拿到「真实接口形态 / 必需请求头 / 活动结束时间等字段」，而不是盲猜。产物是 `app_source/`（解包后的前端 + 主进程源码）。

> `app_source/` 是 **逆向产物，不在本仓库内**（存在于 `D:\workbuddy\app_source`），本仓库只收录「解包流程」与「反代实现」。

### 1.1 目标与边界

| 项 | 说明 |
| ------ | ------ |
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
| ------ | ------ | ------ |
| 设备风控头 `X-Device-Token` | `main/tar.js` `buildHeadersWithTuringToken` / `TURING_SHIELD_ID_HEADER="X-Device-Token"` | 反代必须给签到 / 对话请求注入该头，否则上游风控识别为「非真实客户端」 |
| Turing SDK 桥接 | `resources/app.asar.unpacked/native/turing-sdk/index.cjs`（`configure` + `fetchDeviceToken`） | 复用了同一 SDK 给 Python 网关取 token（见 [2.3](#23-设备风控头提供器)） |
| channelId = `109144` | `app_source/cli/product.json` → `turingSdk.channelId` | `turing_helper.js` 默认 channelId |
| 签到链路 | `main/tar.js` `claimDailyCheckin` → `POST /v2/billing/meter/daily-checkin` | 定时任务直接打该端点（见 [docs/TASKS.md](./TASKS.md)） |
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
| ------ | ------ | ------ |
| `POST /v1/chat/completions` | OpenAI Chat（流式） | 已支持 |
| `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） | 已支持 |
| `POST /v1/messages` | Anthropic Messages（适配 Claude Code / CC Switch） | 已支持 |
| `GET /v1/models` | 实时拉取后端模型，失败回退内置列表 | 已支持 |
| `GET /v1/balance` | 当前账号积分额度 | 已支持 |
| `GET /health` | 健康检查（含余额摘要）。单端口部署下为 **`/gw/health`**；admin 侧另有独立的 `/health`（返回账号池摘要，不需鉴权） | 已支持 |

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

