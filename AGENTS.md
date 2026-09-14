# AGENTS.md

本文件是**跨 AI 编程工具的单一真相源**（source of truth）。

- **原生读取**：Codex CLI、Cursor、Pi、Qwen Code、CodeBuddy（无 `CODEBUDDY.md` 时自动回退）、GitHub Copilot（仅部分 Copilot 功能）
- **需要桥接**：Claude Code 通过 `CLAUDE.md`、Gemini CLI 通过 `GEMINI.md` 中的 `@AGENTS.md` 导入同一份内容
- **不读取**：通义灵码、Qoder、Trae 走各自的 `.lingma/rules`、`.qoder/rules`、`.trae/rules` 目录，本项目暂未适配

> **只改这里。** 不要给每个工具各写一份约定，否则必然漂移。

---

## 一、这个项目是什么

把 **WorkBuddy / CodeBuddy 桌面端**的登录态，转成 **OpenAI / Anthropic 兼容 API**，并在其上提供多账号共享网关。

| 组成 | 职责 |
| ------ | ------ |
| `core/` | 协议转换：桌面端登录态 → OpenAI / Anthropic / Responses |
| `admin/` | 多账号账号池 + API Key 网关（配额、限流、故障转移、管理后台） |
| `scripts/` | 每日签到等自动化脚本 |

**免责**：本项目依赖第三方桌面端的登录态与其服务端接口，仅供**个人自用与学习**。使用前请自行确认符合上游服务条款，不要用于商业转售或大规模分发。

---

## 二、技术栈与依赖

| 项 | 说明 |
| ------ | ------ |
| 语言 | Python 3.10+（推荐 3.12）；`turing_helper.js` 需 Node.js LTS |
| Web | FastAPI + uvicorn（全异步） |
| 存储 | MySQL 8.x（账号池 / 用量）、Redis 7.x（**可选**，缺失时限流自动降级） |
| ORM | SQLAlchemy 2.0 |
| 依赖清单 | `requirements.txt`（**不含 pytest**，跑测试需自行安装） |

数据库连接、密钥等全部来自根目录 `.env`（`python-dotenv` 加载，**不覆盖已存在的环境变量**）。
首次运行：`cp .env.example .env`，然后按注释填自己的值。

---

## 三、常用命令

```bash
pip install -r requirements.txt

python main.py                  # 单端口一体化：管理后台 + 网关 + 内嵌 converter
                                # 默认监听 0.0.0.0:8790

python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790   # 仅管理后台

pip install pytest && pytest tests/     # 回归测试
```

启动时会检测并**告警弱密钥 / 弱口令**，部署前请覆盖 `ADMIN_JWT_SECRET` 与 `ADMIN_PASSWORD`。

Windows 下安装为系统服务用 `service_admin.py`（服务宿主，与 `main.py` 共用同一 ASGI 应用）。
**注意：重启服务需要管理员权限的终端**，普通终端执行 `sc stop` 会报「拒绝访问」。

---

## 四、代码结构

| 路径 | 职责 |
| ------ | ------ |
| `core/converter.py` | 转换核心：登录态 → 各协议 |
| `core/anthropic_adapter.py` | Anthropic Messages ↔ OpenAI Chat 消息转换 |
| `core/responses_projection.py` | OpenAI Responses 协议投影 |
| `admin/routers/proxy.py` | 网关主循环：选号、重试、记账 |
| `admin/server.py` | FastAPI 应用装配 |
| `admin/models.py` | ORM：账号池 / API Key / 用量 |
| `admin/scheduler.py` | 后台定时任务（余额刷新、签到） |
| `tests/` | pytest 回归测试 |

---

## 五、约定

- **注释与文档用中文，标识符用英文。**
- **提交信息遵循 conventional commits**，例：`fix(anthropic): 修正工具调用消息顺序`。
- **每次改动都要在 `CHANGELOG.md` 的 `[未发布]` 一节补条目。** 项目**不打版本标签**，条目以日期为标题，格式参考 Keep a Changelog。
- 新增协议差异应做成 `core/` 下的适配器，**不要在 `proxy.py` 主循环里堆 `if`**。
- 改动对外行为（端点、参数、报错码）时，同步更新 `README.md` 对应章节。

---

## 六、API 契约（对接客户端时最容易搞错）

| 客户端类型 | base_url 写法 |
| ------ | ------ |
| OpenAI 兼容 | `http://<host>:8790/v1` |
| Anthropic / Claude Code | `http://<host>:8790` —— **不能带 `/v1`** |

网关有两套鉴权，别混用：

- **账号池 Key**（存于 MySQL，67 字符）→ 走 `/v1/*`，带配额管理
- **内嵌 Key**（`.env` 的 `CONVERTER_API_KEY`，51 字符）→ 走 `/gw/*`

脱敏开关默认**不对称**：`ADMIN_ANTHROPIC_DESENSITIZE=1`、`ADMIN_OPENAI_DESENSITIZE=0`。调整前先确认会否压掉客户端需要的 system prompt 内容。

---

## 七、容易踩的坑

- **错误码 `11128` 是内容审核拦截，不是渠道或账号故障。** 排查时不要换号、不要改配置。也不要拿 `Reply with exactly: OK` 这类超短 prompt 做连通性验证——极易触发审核。
- **错误码 `11148 tool calls and tool results do not match`**：OpenAI 协议要求 `tool` 消息**紧跟**带 `tool_calls` 的 assistant 消息。改 `anthropic_adapter` 的 user 消息转换时，**不要**把文本块插到 `tool` 消息前面。
- `tests/` 里部分回归测试被 `.gitignore` 排除，**只在本地保留、不进仓库**。因此「某测试文件已新增」的说明可能在实际 clone 中找不到该文件。
- 容器化时，`ADMIN_DATABASE_URL` 若写 `127.0.0.1` 会指向容器自身，需改成宿主地址或服务名。
- auth 目录以 `:ro` 只读挂载时，token 刷新的原子写会失败。
- 后端未回传用量时按 `completion_tokens × COST_PER_TOKEN` **估算**扣费，是经验值，不是精确账单。

---

## 八、不要做的事

- **不要提交 `.env`**，也不要把任何凭据、账号、代理地址、本机绝对路径写进代码或文档——本仓库对外公开。
- 不要硬编码密钥或默认口令来「让跑得起来」，用 `.env`。
- 不要为了绕过报错而放宽鉴权、验签或参数校验。
- 不要把 `.env.example` 里的占位值当成可用的生产配置。

---

## 九、进一步阅读

| 想知道 | 看哪 |
| ------ | ------ |
| 完整架构、逆向过程、部署方式 | `README.md` |
| 各环境变量含义 | `.env.example`、`README.md` §3.4 |
| 历史变更与原因 | `CHANGELOG.md` |
| 部署到 Windows / Sealos | `docs/DEPLOY_WINDOWS.md`、`docs/DEPLOY_SEALOS.md` |
| 环境准备 | `docs/ENV_SETUP.md` |
