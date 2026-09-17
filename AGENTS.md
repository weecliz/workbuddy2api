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
- **临时文件一律放 `.tmp/`**（已在 `.gitignore` 中忽略），不要放在项目根目录或其他受跟踪目录。见下方《临时文件》。

---

## 五之二、临时文件

用于提交拆分、一次性校验脚本、中间产物等的**临时文件一律放 `.tmp/`**：

```
<项目根>/
└── .tmp/          # 已被 .gitignore 忽略，不进入版本库
    ├── split.py          # 例：把一个改动拆成多个提交的脚本
    ├── smoke.py          # 例：接口冒烟检查
    └── out.txt           # 例：脚本输出
```

- **新建临时文件前先 `mkdir -p .tmp`**；用完**自行删除**，不要累积。
- **不要**把临时文件写成 `.ctmp/`、`_test_xxx.py`、`verify_*.js` 这类散落在根目录的名字——它们很容易被 `git add .` 误加入暂存区。
- 确实需要留在仓库里的脚本，放到 `scripts/`（功能代码）；一次性的一次性不要污染那里。
- 写入 `.tmp/` 下的内容**不进版本库**，所以不要把它当作交付物；需要交付的产物请放到 `docs/` 或 `scripts/`。

> 为什么写进本文件：曾发生两次误跟踪——`.ctmp/` / `.ctmp225/` 下的 14 个临时脚本曾被 `git add` 进暂存区，靠人工核对 `git status` 才拦下。

### 铁律：临时脚本绝不能碰真实数据库

`admin/db_config.py` **在模块导入时**就解析 `.env` 并实例化全局单例 `db_config`。
所以只要写下：

```python
from admin.server import app          # ← 这一步就已经连上真实库了
c = TestClient(app)
```

后面的任何写接口（`POST /api/accounts`、走 `/v1/chat/completions` 的记账……）
都会**真落库、真消耗账号额度**。

> 真实事故：曾用这种方式验证接口，把一个测试账号写进了生产 MySQL，
> 并在 `usage_logs` 里留下真实用量记录（无法可靠区分、也无法撤销）。

**做临时验证时必须隔离，三种合法做法：**

1. **写 pytest 用例**（推荐）：`tests/conftest.py` 已在导入 `admin.*` 之前把
   `ADMIN_DATABASE_URL` 指向 `.tmp/test.db`，并有守门夹具在非隔离时直接报错中止。
2. **临时脚本复用同一隔离**：脚本顶部**先** `import tests.conftest`，再导入 `admin.*`。
3. **只读验证**：只用 `SELECT`（`db.query(...).count()` / `.all()`），不调任何写接口。

验证脚本跑完请确认一句话：**真实库的条数没变**。

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
- **不要为了消除 lint / 类型检查告警而改代码。** 判定标准与例外见《八之二》。
- **不要把临时脚本 / 中间产物写进项目根目录或受跟踪目录**（如 `.ctmp/`、`_test_*.py`、`verify_*.js`）——用 `.tmp/`，它已被忽略。
- **不要用 `TestClient(app)` 直接跑写接口做验证**——那会连上 `.env` 里的真实库。用 `tests/conftest.py` 的隔离，或只做只读查询。
- 不要把 `.env.example` 里的占位值当成可用的生产配置。

---

## 八之二、lint 告警的处理边界（硬性）

静态检查的告警**不是任务清单**。改代码前先问一句：

> **这个改动除了让告警消失，还有其他价值吗？**
> 答不出具体的一条，就不要改。

### 可以改（三种情形）

1. **它指出了真实缺陷。** 例：`reportUndefinedVariable` 报出 `AccountSession` 未定义
   （真事故：`_fetch_real_credits` 因裸用未导入的名字而静默失效）；
   又例：`token != key` 被指出可用 `hmac.compare_digest` 消除时序旁路。
2. **它指出的写法真的更健壮。** 例：原子写外面包 `try/except OSError`，
   把「写盘失败」变成带路径的可定位错误。
3. **它在同一项目里已有一致做法。** 例：改 `converter._check_auth` 时对齐
   `admin/security.py` 里已统一使用的 `compare_digest`。

### 不要改（错误示范）

- **为消噪而抽无用 helper。** 曾把 `int(time.time() * 1000)` 改成新增的
  `_now_ms()`——只为绕开「裸 `int()` 可能在 try 外」的规则，取值完全等价、
  零收益，反而多一个新符号要维护。**同理适用于 `_now_s()`。**
- **在已被保证的调用上叠 try/except。** 如 `float(m.group(1))` 的入参由前置正则
  `[0-9]+(\.[0-9]+)?` 限定，不可能抛 `ValueError`；加 try 前后都是死代码。
- **把规则误报当缺陷修。** 某些规则按裸调用模式匹配（`open($$$)` / `int($$$)` /
  `float($$$)`），不做数据流与跨函数分析，因此看不出「已被 try 包住」或
  「恒不抛」。这类告警的正确处置是**留着**，不是改代码。

### 为什么写成硬性

> 真实事故：曾用一整轮把 `converter.py` 的 7 条规则误报逐个「修」掉，
> 引入 `_now_ms`/`_now_s` 两个无用 helper、一个**未使用的 `hashlib` 导入**
> （pyflakes 当场报 `imported but unused`），还在注释里留了错别字。
> 全程零真实收益，净增维护面。最后全部回退，只保留 `hmac.compare_digest`
> 那一处有独立价值的修复。

**告警清零不是目标，「代码更对」才是。** 若确实需要屏蔽某条规则，
用 `lens_diagnostic_mark` 记录理由（说明为何是误报），并在 PR/提交信息里讲清；
**不要**为了好看去改无关代码。

---

## 九、反虚构前提（硬性）

> 与 `~/.pi/agent/AGENTS.md` 的同名章节一致（全局约束，本仓库同样适用）。

**核心规则**：任何“用户说过 / 要求过 X”的表述，必须能指向**具体的一条消息**。指不出，就不得据此行动。

- **禁止伪造用户原话**：不得用“你说过…”“你之前提到…”这类措辞起头去改代码，除非该内容确实出现在用户消息里。
- **不确定就复述并提问**：把“你说数据不全”改写成“我理解是数据不全，对吗？”，等确认后再动手。
- **交付后停下等确认**：不要自造下一个问题来填补空白。
- **长推理要分段**：不在单个 thinking 块里自我辩论上万字——中途缺少外部校验点，容易把臆想当成既定事实。
- **主动更正**：发现虚构过前提要明确提出，不要当作已发生的事实继续引用。

**为何写进本仓库**：曾发生过一次真实事故——在日期约束功能已完成并验证通过后，
“用户说数据全部不对了”这一前提被凭空写进思考块（该消息在会话记录中并不存在，
`user` 角色命中数为 0），导致后续据此起了十几次真实浏览器排查一个不存在的问题。

---

## 十、进一步阅读

| 想知道 | 看哪 |
| ------ | ------ |
| 完整架构、逆向过程、部署方式 | `README.md` |
| 各环境变量含义 | `.env.example`、`README.md` §3.4 |
| 历史变更与原因 | `CHANGELOG.md` |
| 部署到 Windows / Sealos | `docs/DEPLOY_WINDOWS.md`、`docs/DEPLOY_SEALOS.md` |
| 环境准备 | `docs/ENV_SETUP.md` |
