# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机 / 局域网可直接使用的 **OpenAI / Anthropic 兼容 API**，并提供一个 **多账号代理共享平台**（账号池自动切换、独立 API Key、按 Key 配额、用量记账）。

`workbuddy2api` 不负责登录、不模拟桌面端、不替你执行工具。它只做三件事：

1. 读取本机登录态并注入完整的鉴权头（含设备风控头 `X-Device-Token`）
2. 在 OpenAI / Anthropic 协议与腾讯后端协议之间转换
3. 对 Codex CLI 这类长上下文 agent 请求做后端友好的压缩投影

---

## 功能与特点

- **三协议网关**：OpenAI Chat / Responses（Codex CLI）/ Anthropic Messages（Claude Code），共用同一批 Key、同一套配额与记账
- **多账号共享平台**：账号池自动切换（剩余最多优先 / LRU）、独立 API Key、按 Key 积分上限、超额 402 拦截、用量记账落库
- **稳定性设计**：账号级 3 次换号重试、错误分类差异化冷却、防撞号窗口、账号状态持久化
- **OAuth 一键加号**：浏览器完成官方登录即入池，不需要桌面端
- **自动化任务**：每日签到、成长任务全自动点亮领奖、猫猫旅行、活跃上报，全部接入可配置调度框架
- **设备指纹隔离**：账号 uid 稳定哈希派生设备标识，同号固定同设备、多号彼此隔离，容器部署同样生效
- **上游真实账单统计**：直读官方用量接口，任意区间按天 / 按模型 / 分账号汇总
- **零依赖起步**：默认 SQLite，克隆即可跑

各能力的实现细节、逆向依据与配置项见下方文档索引。

---

> 📝 **更新日志**：版本变更、新增能力与修复记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 快速上手

只有三步（默认用 SQLite，**不需要装任何数据库**）：

```bash
cp .env.example .env          # 1. 复制配置（默认值即可先跑通）
pip install -r requirements.txt   # 2. 装依赖
python main.py                # 3. 启动，默认监听 0.0.0.0:8790
```

然后用浏览器打开 **`http://127.0.0.1:8790/admin`**，登录：

| 用户名 | 密码 |
| ------ | ------ |
| `admin` | `admin123` |

> ⚠️ **部署前请务必改掉**：设置 `.env` 的 `ADMIN_PASSWORD` 与 `ADMIN_JWT_SECRET`（启动时会告警弱口令）。

登录后做两件事就能接入客户端：

1. **「账号」页** → 批量上传 / 扫描本机 / OAuth 添加，导入 WorkBuddy 登录态
2. **「密钥」页** → 创建一把 Key，复制走

更完整的三种运行方式见 [四、环境安装与项目运行](#四环境安装与项目运行)。

---

## 关键信息速查

| 你要的 | 值 |
| ------ | ------ |
| 管理后台 | `http://<host>:8790/admin` |
| OpenAI 系客户端 base_url | `http://<host>:8790/v1` |
| Anthropic 系（Claude Code）base_url | `http://<host>:8790` —— **不带 `/v1`** |
| 鉴权头 | `Authorization: Bearer <Key>` 或 `X-API-Key: <Key>` |
| 账号池 Key（走 `/v1/*`） | 后台「密钥」页创建，**67 字符**，带配额与记账 |
| 内嵌 Key（走 `/gw/*`） | `.env` 的 `CONVERTER_API_KEY`，**51 字符**，无配额 |
| 默认数据库 | SQLite（零依赖，数据在 `./data/workbuddy_admin.db`） |
| 上游报错 `11128` | **内容审核拦截**，不是账号或渠道故障——换号、改配置都没用 |

不确认用哪套协议？**OpenAI 系填 `/v1`，Anthropic 系不填**——两种 SDK 都会自己拼后面的路径。

> ⚠️ **两把 Key 别混用**：长度一眼可辨（**67 = 账号池**、**51 = 桌面端单账号**）。
> 把 `/gw` 的 Key 换成后台那把，它依然只烧桌面端登录的那个账号。

### 最小接入示例

```bash
# OpenAI 协议（Cherry Studio / LobeChat / NextChat / Open WebUI 等）
export OPENAI_BASE_URL=http://127.0.0.1:8790/v1
export OPENAI_API_KEY=sk-你的67位Key
```

验证连通性（**网关固定返回流式 SSE**，所以别传 `"stream": false`，也请用长一点的 prompt）：

```bash
curl -N $OPENAI_BASE_URL/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash",
       "messages":[{"role":"user","content":"用一句话介绍你自己"}]}'
```

正常时会逐段输出 `data: {...}`，最后以 `data: [DONE]` 结束。

> ⚠️ 不要用 `Reply with exactly: OK` 这类超短 prompt 做连通性验证——它极易触发上游内容审核。

各客户端的完整配置见 [六、客户端接入](#六客户端接入)。

---



---

## 项目运行截图

<img src="./images/img_1.png">
<img src="./images/img_2.png">
<img src="./images/img_3.png">

---

## 环境安装与项目运行

### 4.1 前置依赖

| 依赖 | 用途 | 版本 |
| ------ | ------ | ------ |
| Python | 运行 converter / admin | 3.10+（推荐 3.12） |
| Node.js | 设备风控头 `turing_helper.js`（require 桌面端 SDK） | 任意 LTS |
| SQLite（默认） / MySQL / IBM Db2 | admin 账号池 / 用量库 | 三选一，由 `ADMIN_DB_TYPE` 切换。**默认 SQLite**（零依赖，无需装任何数据库，数据落在 `./data/workbuddy_admin.db`）；高并发生产建议 MySQL 8.x（`root/root`，库名 `workbuddy_admin`）或 Db2 LUW 11.x（`db2inst1`，库名 `WBADMIN`） |
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

`requirements.txt`：`fastapi` · `uvicorn[standard]` · `httpx` · `sqlalchemy>=2.0` · `pymysql`（MySQL 驱动）· `redis` · `python-multipart` · `PyJWT` · `cryptography`
按数据库驱动三选一：MySQL 用 `pymysql`，DB2 用 `ibm_db_sa`，**SQLite 无需安装任何东西**（见 [docs/DB_SUPPORT.md](docs/DB_SUPPORT.md)）。

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

一键脚本（本机已配好）：`scripts/start_admin.bat` 启动，`scripts/stop_admin.bat` 停止。
停止脚本按端口找到进程树并优雅关闭（`--port` / `--force` / `--list` 可选），
因为该服务是 `main.py → uvicorn` 的父子结构，只杀父进程会把子进程连同端口一起留下。

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

见 [docs/CONFIG.md](./docs/CONFIG.md) 第九节。

---


---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/CONFIG.md](./docs/CONFIG.md) | **配置参考**：全部环境变量与命令行参数，按用途分类 |
| [docs/CLIENTS.md](./docs/CLIENTS.md) | 客户端接入：Codex CLI / Claude Code / OpenAI 系完整配置 |
| [docs/TASKS.md](./docs/TASKS.md) | 定时任务与运维：签到、成长任务、日志与排障 |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 管理平台架构：稳定性设计、路由总览、OAuth 全流程、身份画像、设备指纹、custom 工具 |
| [docs/REVERSE_ENGINEERING.md](./docs/REVERSE_ENGINEERING.md) | 逆向工程：桌面端解包步骤、关键发现、网关核心架构 |
| [docs/DB_SUPPORT.md](./docs/DB_SUPPORT.md) | 数据库支持：SQLite / MySQL / Db2 差异与切换 |
| [docs/ENV_SETUP.md](./docs/ENV_SETUP.md) | 环境准备 |
| [docs/DEPLOY_WINDOWS.md](./docs/DEPLOY_WINDOWS.md) · [docs/DEPLOY_SEALOS.md](./docs/DEPLOY_SEALOS.md) | 部署到 Windows / Sealos |
| [CHANGELOG.md](./CHANGELOG.md) | 更新日志（按日期归档） |

---

## 项目结构

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
├── admin/                    # 多账号管理后台（FastAPI + MySQL/DB2/SQLite + Redis）
│   ├── server.py             # FastAPI 入口、登录、静态页挂载、converter 挂 /gw
│   ├── config.py             # 配置（环境变量覆盖）
│   ├── db_config.py          # 数据库配置中心：类型 + 连接参数 → 连接串
│   ├── db_dialect.py         # 方言适配：MySQL / DB2 / SQLite（引用符、类型映射、系统表）
│   ├── db.py                 # SQLAlchemy 引擎 / 会话 / 建库建表 / 列迁移
│   ├── models.py             # Account / ApiKey / UsageLog / Schedule ORM
│   ├── security.py           # JWT、Key 哈希、配额拦截
│   ├── backend/              # 单账号上游会话（按域拆分）
│   │   ├── session.py        # AccountSession：凭据落盘/回写 + 档案与额度
│   │   ├── checkin.py        # 每日签到
│   │   ├── growth.py         # 猫猫领养 / 旅行 / 连登 / 活跃上报
│   │   ├── usage.py          # 上游真实用量：区间全量拉取 + 汇总/按天/按模型聚合
│   │   └── http.py           # 连接池参数、凭据元信息解析
│   ├── tasks/                # 后台任务实现（一个任务一个文件）
│   │   └── daily_checkin.py / cat_travel.py / activity_report.py / common.py
│   ├── scheduler.py          # 调度框架：轮询 schedules 表并分发到 admin/tasks/
│   ├── turing_token.py       # Python 侧 X-Device-Token 提供器（subprocess 调 helper）
│   ├── oauth_login.py        # OAuth 设备授权登录（浏览器登录换凭据，不需桌面端）
│   ├── routers/              # accounts / oauth / keys / proxy / schedules / logs / usage / sync / models
│   └── static/index.html     # 纯 HTML + TailwindCSS + FontAwesome 管理大屏
├── deploy/                   # Docker 部署配置（build context 是仓库根目录）
│   ├── Dockerfile            # converter 独立版（8787，需挂载桌面端 auth）
│   ├── Dockerfile.admin      # admin 独立版（8790，Sealos / 容器平台）
│   └── docker-compose.yml
├── docs/                     # 部署文档：DEPLOY_WINDOWS / DEPLOY_SEALOS / ENV_SETUP
├── examples/                 # 客户端接入示例（手动合并进自己的配置，脚本不自动改写）
│   └── codex-codebuddy.example.toml  # Codex CLI / Claude Code / 其它客户端接入片段
├── scripts/                  # 本机一键脚本：start_admin / stop_admin / start_converter / 服务安装卸载
│   ├── start_admin.bat       # 启动（参数原样转发给 main.py）
│   ├── stop_admin.bat        # 停止（按端口找进程树，优雅关闭，可选 --force）
│   ├── db_probe.py           # 启动前数据库自检（批处理共用，按方言检查驱动与连通性）
│   └── _proc_tree.ps1        # stop_admin 的进程树查询辅助（查后代 / 祖先）
├── tests/                    # pytest：代理重试骨架 + 猫猫旅行状态机 + 用量聚合
└── README.md

# 逆向产物（不在本仓库，存在于 D:\workbuddy）
D:\workbuddy\app_source\      # cli / main / preload / renderer 解包源码
D:\workbuddy\resources\app.asar.unpacked\native\turing-sdk\   # 设备风控原生模块（运行时不写死此路径，由 turing_helper.js 自动发现）
```

---


## 免责声明与协议

**与上游服务提供方**：本项目仅用于个人学习与研究，与腾讯、WorkBuddy / CodeBuddy、
OpenAI、Anthropic **无任何官方关联**，也未获得其授权或背书。请仅在你合法拥有订阅的
前提下使用，并自行承担风险。

**与上游开源项目**：本仓库最初派生自
[xiaofan6ya/workbuddy2api](https://github.com/xiaofan6ya/workbuddy2api)，
此后为独立开发与维护。上游项目的作者不对本仓库的代码、行为及其产生的任何问题负责，
本仓库的改动亦不代表上游项目的立场。上游同样以 MIT 协议发布，原版权声明保留在
[LICENSE](./LICENSE) 中。

协议：[MIT](./LICENSE)

> 致谢：本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。
