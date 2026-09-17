# 配置参考

> 所有配置项集中索引：数据库、鉴权、OAuth、身份画像、指纹、脱敏、定时任务、
> converter 命令行参数。逐项含义与默认值以 `.env.example` 内注释为准，本文只做
> 分类导航与易错点提示。

## 一、数据库（admin）

| 项 | 说明 |
| --- | --- |
| `ADMIN_DB_TYPE` | `sqlite`（默认，零依赖）/ `mysql` / `db2`，一处切换 |
| `ADMIN_DB_HOST` / `PORT` / `USER` / `PASSWORD` / `NAME` / `SCHEMA` | 分项连接参数 |
| `ADMIN_DATABASE_URL` | 直接给连接串，优先级最高 |
| 命令行指定 | `python main.py --db-type mysql --db-password xxx`（只影响本次启动） |

详细差异（引用符 / 类型映射 / 迁移行为 / 启动自检）见 [docs/DB_SUPPORT.md](./DB_SUPPORT.md)。

## 二、管理与鉴权

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | 后台登录；启动时告警弱口令，**部署必改** |
| `ADMIN_JWT_SECRET` | 无 | ≥32 字节，**部署必改** |
| `ADMIN_JWT_EXPIRE_HOURS` | - | JWT 有效期 |
| `ADMIN_PORT` | `8790` | 监听端口 |
| `ADMIN_REDIS_URL` | `redis://127.0.0.1:6379` | Key/配额缓存；缺失时限流自动降级进程内计数 |
| `ADMIN_COST_PER_TOKEN` | - | 上游未回账时的扣费估算系数 |
| `ADMIN_ACCOUNT_SELECT` | `remain` | 选号策略：剩余最多优先 / `lru` 最久未用 |
| `ADMIN_CLIENT_AUTH_DIR` | - | 后台「扫描本机 / 注入本机」的凭据目录 |

## 三、客户端接入相关（Anthropic / OpenAI 端点）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_ANTHROPIC_MODEL_OPUS` / `_SONNET` / `_HAIKU` | `deepseek-v4-pro` / `glm-5.2` / `glm-5.3-flash` | Claude 模型名三档映射。**不要设成 `auto`**（语义是取第一个启用模型，可能挑到图像模型） |
| `ADMIN_ANTHROPIC_DESENSITIZE` | `1` | harness 脱敏开关，**关掉基本发不出去**（11128 内容审核） |
| `ADMIN_ANTHROPIC_NO_COMPACT` | `0` | 只零宽脱敏、跳过 harness 压缩 |
| `ADMIN_OPENAI_DESENSITIZE` | `0` | 把同一套脱敏应用到 `/v1/chat/completions` 与 `/v1/responses`；仅长 harness 客户端（Pi、claude-code-router 等）需要开 |

## 四、内嵌 `/gw` 网关（converter）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONVERTER_API_KEY` | **必设** | 留空 = 完全不鉴权，而服务默认监听 `0.0.0.0` |
| `CODEBUDDY_AUTH_DIR` | - | converter 读取桌面端凭据的目录。**与 `ADMIN_CLIENT_AUTH_DIR` 是两个变量**；Windows 服务方式运行时两者都写绝对路径 |
| `CONVERTER_DESENSITIZE` / `CONVERTER_LOG` | - | 同 converter 命令行的 `--desensitize` / `--log` |

## 五、OAuth 一键加号（`ADMIN_OAUTH_*`，均可省略）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_OAUTH_PLATFORM` | `CLI` | 官方 CLI 与桌面端都是 `CLI` |
| `ADMIN_OAUTH_USER_AGENT` | `CLI/2.148.0 CodeBuddy/2.148.0` | 随客户端版本变化，建议按本机实际版本调整 |
| `ADMIN_OAUTH_ORIGIN` | `https://www.codebuddy.cn` | Origin / Referer |
| `ADMIN_OAUTH_TTL` | `600` | 登录会话存活秒数 |
| `ADMIN_OAUTH_TIMEOUT` | `15` | 单次上游请求超时秒数 |

流程与安全设计见 [docs/ARCHITECTURE.md](./ARCHITECTURE.md) §3.7。

## 六、客户端身份画像与设备指纹

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_UPSTREAM_CLIENT_KIND` | `workbuddy` | 凭据 `auth.domain` 缺失时的兜底身份 |
| `ADMIN_UA_WORKBUDDY` | `CLI/5.3.14 WorkBuddy/5.3.14` | 桌面端 UA；版本号按官方规则推断，升级客户端后请同步 |
| `ADMIN_UA_CODECLI` | `CLI/2.148.0 CodeBuddy/2.148.0` | CLI UA（本机实测确认） |
| `ADMIN_DEVICE_FINGERPRINT` | `on` | 稳定指纹三头总开关；`off` 恢复升级前行为 |

机制说明见 [docs/ARCHITECTURE.md](./ARCHITECTURE.md) §3.8 / §3.9。

## 七、Turing SDK（X-Device-Token）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WORKBUDDY_TURING_SDK_DIR` | 自动发现 | SDK 目录（特殊安装位置时可显式覆盖） |
| `WORKBUDDY_TURING_CHANNEL_ID` | `109144` | channelId |
| `WORKBUDDY_PRODUCT_NAME` | `WorkBuddy` | - |
| `WORKBUDDY_VERSION` | `2.0.0` | - |

## 八、定时任务

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_GROWTH_TASK_ENABLED` | `1` | 成长任务总开关；`0` 一键停用 |
| `ADMIN_GROWTH_REPORT_GAP` | `1.5` | 同号事件上报条间隔（秒） |
| `ADMIN_ACTIVITY_REPORT_COUNT` | `5` | 每号每次活跃上报条数 |

任务类型、风控口径与调度配置见 [docs/TASKS.md](./TASKS.md)。

## 九、converter 命令行参数（方式 B 独立运行）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize`，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |
