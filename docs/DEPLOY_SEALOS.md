# 仅管理后台（admin 独立）部署到 Sealos

> 目标：把 `main.py` 里的「管理后台 + 托管网关」这部分单独跑在 Sealos（如 `https://gzg.sealos.run/`）上，
> 让任意电脑用 `https://<你的域名>/v1` + `sk-xxx` 调用共享号池，**不需要** WorkBuddy 桌面端。
> 部署载体是仓库新增的 `Dockerfile.admin`（不影响根目录原有的 `Dockerfile`）。

---

## 0. 结论

**可以，而且这个项目天然适合 Sealos。**

| 条件 | 本项目情况 | 结论 |
| --- | --- | --- |
| 是否无状态 | 全部状态（账号池、Key、模型配置、用量、调度）都在 MySQL | ✅ 不需要持久化卷 |
| 端口 | `8790` 单端口承载后台 + `/v1/*` 网关 | ✅ 应用管理填一个端口即可 |
| 是否依赖桌面端 | 方式 B 不依赖（桌面端 `.info` 只是被采集对象，存进库后即自给自足） | ✅ 可上云 |
| 是否依赖本机文件 | 仅「扫描本机 / 注入本机」两个可选功能 | ✅ 云端失效但不影响主流程 |
| HTTPS | Sealos 开外网访问自动签发证书 | ✅ 顺带解决了「裸 HTTP 传 Key」的问题 |

---

## 1. 部署前必须知道的 6 个硬约束

这 6 条是读源码确认过的，不是泛泛而谈，直接决定你怎么打包和配置。

1. **「admin 独立」在运行期仍然需要 `converter.py`。**
   `admin/backend.py:14` 是 `from converter import CredentialManager`，
   `admin/server.py:27` 也会 `from converter import app`。
   只打包 `admin/` 目录 → 容器启动直接 `ModuleNotFoundError: No module named 'converter'`。
   → 所以镜像里必须有：`admin/` + `converter.py` + `responses_adapter.py` +
   `responses_projection.py` + `anthropic_adapter.py` + `desensitize.py`（`Dockerfile.admin` 已按此写好）。

2. **MySQL 是硬依赖，Redis 不是。**
   `admin/server.py:70` 的 startup 会执行 `ensure_database()` + `init_db()`（建库 + 建 6 张表 + 列迁移，
   两步都幂等，**不需要你手动建库**）。
   Redis 见 `admin/ratelimit.py:18-19`，连接超时 1 秒，连不上就自动降级为进程内内存计数，功能不变。

3. **必须单实例、单 worker。**
   `admin/scheduler.py:start_scheduler()` 起的定时任务线程**没有任何跨进程锁**。
   多副本 / 多 worker 会让「每日签到」等任务被并发执行 N 次（会真的打上游接口，有风控风险）。
   → Sealos 应用管理里实例数填 `1`，**不要开弹性伸缩**。

4. **Linux 上必然拿不到 `X-Device-Token`。**
   Turing Shield SDK 是 win32/darwin 专有原生模块（`turing-sdk/index.cjs` 显式判断 `process.platform`）。
   缺该头的后果是：功能正常，但**敏感请求更容易被上游风控拦截**。这是上云最大的功能代价，无解。
   （代码有优雅降级：只 WARNING 一次 + 60 秒负缓存，不会拖慢吞吐。）

5. **两个功能在云端失效（预期行为，不用修）**：
   - 「扫描本机账号 / 注入到本机」：`admin/config.py:36` 的 `CLIENT_AUTH_DIR` 默认是
     `%LOCALAPPDATA%\...`，Linux 上 `expandvars` 不展开 → 扫到空列表，不会报错。
   - 内嵌 `/gw/*`：`CONFIG["cred"]` 只在 `converter.py main()` 里赋值，而这里是通过 import 挂载的，
     `main()` 从不执行 ⇒ `/gw/v1/chat|balance|responses` 全部 503；只有 `/gw/v1/models`
     和 `/gw/health` 能通（前者回退内置模型表，且因为 api_key 为空而**无鉴权**，仅泄漏模型名）。
     云端号池在 MySQL 里，`/gw` 拿不到凭据，不会泄漏账号。
     → 想彻底关掉它，见 §8 的可选加固。

6. **首次启动会自动播种 3 个定时任务**（`scheduler.py:seed_defaults`）：
   整点刷新平台总积分（60 分钟）、每日同步模型列表（1440 分钟）、每日签到领取积分（1440 分钟）。
   也就是说**部署完它自己就开始跑任务了**，如果不想让云端定时签到，上线后先去「定时任务」页停用。

---

## 2. 目标架构

```
互联网客户端 (任意电脑)
   │  https://xxxxx.gzg.sealos.run/v1/chat/completions
   │  Header: Authorization: Bearer sk-xxxxxxxx
   ▼
Sealos Ingress（自动 TLS 证书，终止 HTTPS）
   │  http → 容器 8790
   ▼
容器 workbuddy2api-admin（本仓库 Dockerfile.admin，单进程单 worker）
   ├── /admin           管理后台前端 + API
   ├── /v1/*            托管网关（Key 校验 / 配额 / 用量记账）← 对外主入口
   └── /gw/*            内嵌 converter（云端为死代码，恒 503）
   │
   │  内网连接（不开公网）
   ▼
Sealos MySQL 实例（号池 accounts / api_keys / model_configs / usage_logs / schedules / system_settings）
```

---

## 3. 第一步：在 Sealos 建 MySQL

1. 打开 `https://gzg.sealos.run/`，登录后进入你的工作空间。
2. 打开 **数据库** 应用 → **新建数据库** → 选 **MySQL**（版本选 8.0.x）。
3. 规格建议：验证阶段 `0.5 核 / 1 GB / 10 GB` 即可（数据库只存 JSON 凭据和日志）。
4. 创建完成后，在详情页记下 **内网连接地址**、端口、用户名、密码、默认库名。
   - **应用和数据库都在 Sealos 上时用「内网地址」，不要开公网访问。**
5. （可选）在 **数据库 → Redis** 也建一个，用于登录防爆破计数跨实例共享。
   不建也没问题，会自动降级成内存计数。

> ⚠️ **密码里的特殊字符必须 URL 编码。**
> `admin/db.py:ensure_database()` 是用字符串劈分解析 URL 的
> （`rest.split("@", 1)` + `split("/")[-1]`），密码中的 `@` `/` `:` `#` 会让它错位
> —— 实测 `root:p@ss/word@host:3306/db` 会被解析成「密码=p、主机=ss」。
> Sealos 生成的密码是随机串，**大概率含特殊字符**，务必编码：
>
> ```bash
> python -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" '你的密码'
> ```
>
> 得到 `p%40ss%2Fword` 这种形式后再拼进连接串。

---

## 4. 第二步：拿到镜像

Sealos 应用管理支持国内外所有镜像仓库，但**必须先有一个可拉取的镜像**。两条路：

### 路线 A：DevBox 构建（推荐 —— 本机不需要装 Docker）

适合本机没有 Docker 的情况。

1. Sealos 控制台 → **DevBox** → 新建项目 → 框架选 **Python**。
2. 连上终端（Web 终端或本地 VS Code 通过 DevBox 插件 SSH），把项目源码放上去：

   ```bash
   cd ~/project
   # 方式一：从 Git 仓库拉
   git clone <你的仓库地址> .
   # 方式二：直接在 DevBox 文件树里上传（不含 .env / *.zip / logs）
   ```

3. 装依赖并本地验证能起来：

   ```bash
   pip install -r requirements.txt
   export ADMIN_DATABASE_URL='mysql+pymysql://<user>:<urlencoded-pwd>@<内网地址>:3306/workbuddy_admin?charset=utf8mb4'
   export ADMIN_JWT_SECRET='<32字节以上随机串>'
   export ADMIN_USERNAME=admin
   export ADMIN_PASSWORD='<强密码>'
   python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790
   # 浏览器打开 DevBox 的端口转发，确认 /admin 能打开、能登录
   ```

4. 改 `entrypoint.sh`（DevBox 用它决定容器怎么启动 —— **直接改成生产启动命令**）：

   ```sh
   #!/bin/sh
   exec python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790 --log-level info
   ```

   > 不要用仓库的 `main.py`：它是「拉起子进程 + tee 日志」的本地开发脚本，
   > 而且 `_interpreter_with_deps()` 里写死了 `C:\Users\Administrator\...` 的 Windows 回退路径
   > （本机用户是 `weichao`，该路径不存在），容器里没必要多一层进程。

5. DevBox 项目详情 → **版本历史 / 发布版本** → 填版本号（如 `v1`）→ 发布。
   系统会把当前环境打包成标准 OCI 镜像，存进 Sealos 自己的 registry —— 不需要你自己建仓库。
6. 发布成功后点 **部署**，会自动跳到应用管理（继续看第 5 节）。

### 路线 B：本地 / CI 构建后推镜像仓库

本机（或 CI）装了 Docker 的话：

```bash
cd D:/Workspace/git/ai/workbuddy2api

# 建议先建 .dockerignore（本仓库目前没有），避免把 130 MB 备份 zip 传进构建上下文：
#   .env
#   .git
#   __pycache__/
#   **/__pycache__/
#   *.pyc
#   *.log
#   logs/
#   images/
#   *.zip
#   .workbuddy/

docker build -f Dockerfile.admin -t <你的仓库>/workbuddy2api-admin:v1 .
docker push <你的仓库>/workbuddy2api-admin:v1
```

`<你的仓库>` 可以是 Docker Hub、阿里云 ACR（个人版免费）、GHCR 等。
**建议用明确的版本 tag，不要依赖 `latest`** —— 应用管理排查文档明确提示过这点。
私有仓库记得在 Sealos 创建应用时填仓库认证信息。

---

## 5. 第三步：在应用管理里创建应用

Sealos 控制台 → 工作空间 → **应用管理** → **创建应用**：

| 字段 | 填什么 |
| --- | --- |
| 应用名称 | `workbuddy-admin` |
| 镜像名称 | 路线 A：发布版本时自动填好；路线 B：`<你的仓库>/workbuddy2api-admin:v1` |
| 部署模式 | **固定实例** |
| 实例数 | **1**（见 §1 第 3 条，别开弹性伸缩） |
| 计算资源 | 0.5 核 / 1 GB 起步（uvicorn 单进程 + httpx 异步足够） |
| 容器端口 | `8790` |
| 协议 / 访问方式 | **https** |
| 外网访问 | **开启** → 自动分配公网域名 + HTTPS 证书 |
| 环境变量 | 见 §6 |
| 存储容量 | **不需要**（无状态） |
| 启动命令 | **留空**（`Dockerfile.admin` 里已定义好 CMD） |

部署后等待状态变成 **Running**，详情页会给出公网地址，形如 `https://xxxxx.gzg.sealos.run`。

---

## 6. 第四步：环境变量清单

在应用管理的「环境变量」里逐条填（值不要带引号）：

| 变量 | 必填 | 说明 / 示例 |
| --- | --- | --- |
| `ADMIN_DATABASE_URL` | ✅ | `mysql+pymysql://<user>:<urlencode(pwd)>@<MySQL内网地址>:3306/workbuddy_admin?charset=utf8mb4`　库名可自定义，启动时会自动创建 |
| `ADMIN_JWT_SECRET` | ✅ | 32 字节以上随机串。**不设会回退到代码里公开的 `dev-insecure-jwt-secret-change-me`，任何人都能伪造管理员 token** |
| `ADMIN_USERNAME` | ✅ | 后台登录名，如 `admin` |
| `ADMIN_PASSWORD` | ✅ | 强密码。默认 `admin123` 且启动时会告警 |
| `ADMIN_REDIS_URL` | ⭕ | 建了 Redis 就填内网地址，没建就不填（1 秒超时后自动降级） |
| `ADMIN_CORS_ORIGINS` | ✅ | 默认是 `*`，公网部署**必须收紧**，如 `https://<你的域名>`；多个用逗号分隔 |
| `ADMIN_PORT` | ⭕ | 默认 8790，与容器端口一致即可，可不填 |
| `ADMIN_BACKEND` | ⭕ | 默认 `https://copilot.tencent.com`，无特殊需求不用改 |
| `ADMIN_ACCOUNT_SELECT` | ⭕ | `remain`（剩余最多优先，默认）/ `lru`（最久未用优先） |
| `ADMIN_UPSTREAM_CLIENT_NAME` | ⭕ | 默认 `WorkBuddy`，透传给上游识别 client |
| `ADMIN_COST_PER_TOKEN` | ⭕ | 上游不回传 credits 时的估算系数，默认 `0.01` |
| ~~`ADMIN_CLIENT_AUTH_DIR`~~ | ❌ | 云端不用填，填了也是死路径 |
| ~~`CODEBUDDY_AUTH_DIR`~~ | ❌ | 这是 converter 独立版用的，admin 版不需要 |

生成随机密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

> 项目根目录的 `.env` **不要**打进镜像、也不要上传到 Sealos 文件里 ——
> env 文件会覆盖系统环境变量（`config.py:19` 的 `load_dotenv` 不覆盖已有变量，
> 但一旦文件里有值就会优先）。在 Sealos 用「环境变量」表单配置是最干净的做法。

---

## 7. 第五步：验证

按顺序过一遍：

1. 应用详情 → 日志，看到 `Uvicorn running on http://0.0.0.0:8790` 且没有 traceback。
2. 浏览器打开 `https://<域名>/admin` → 出现登录页 → 用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录成功。
3. **导入账号**：账号页 → 粘贴桌面端 `.info` 原文（或从本地后台用「同步到线上」推过来），
   确认能刷出余额 / 模型。
   - 若刷余额失败，多半是 `X-Device-Token` 缺失被风控，看日志里的 WARNING 和上游返回码。
4. **建 Key 并调用**：

   ```bash
   curl -s https://<域名>/v1/models -H "Authorization: Bearer sk-xxxx"
   ```

   ```bash
   curl -s https://<域名>/v1/chat/completions \
     -H "Authorization: Bearer sk-xxxx" \
     -H "Content-Type: application/json" \
     -d '{"model":"<模型名>","messages":[{"role":"user","content":"hi"}],"stream":false}'
   ```

5. 后台「用量日志」页应出现刚产生的记录（`usage_logs` 表）。
6. 数据库里确认 6 张表已建好：`accounts` / `api_keys` / `model_configs` / `schedules` / `system_settings` / `usage_logs`。

---

## 8. 公网安全加固清单（必做）

这个项目原来是给「局域网内自己人用」设计的，直接挂公网必须补几项：

| 项 | 现状 | 动作 |
| --- | --- | --- |
| 传输加密 | `main.py:140` 起 uvicorn 没传 ssl 参数，裸 HTTP | ✅ Sealos 开外网访问自动 HTTPS，已解决 |
| CORS | `config.py:78` 默认 `*`，任意网站 JS 都能调 | 🔧 设 `ADMIN_CORS_ORIGINS=https://<你的域名>` |
| 管理员密钥 | 未设 `ADMIN_JWT_SECRET` 时回退到代码里公开的弱密钥 | 🔧 必须设强随机串 |
| 后台口令 | 默认 `admin123` | 🔧 必须改强密码，上线后可在后台改 |
| `/v1/*` 限流 | `ratelimit.py` **只保护 `/api/login`**，网关侧无限流 | 🔧 给每个 Key 设小配额，定期查用量页；Key 泄漏 = 号池被刷干 |
| 来源限制 | 无 IP 白名单，网络可达 + Key 有效即可调用 | 🔧 可选：在 Sealos 侧配访问控制，或自行加反代层 |
| 登录锁定 | 依赖 `X-Forwarded-For` 首个 IP（`ratelimit.py:33`） | ⚠️ 若 Sealos 网关不覆盖客户端传来的 XFF，攻击者可伪造 XFF 轮换绕过锁定 |
| `/gw/*` 噪音 | `/gw/v1/models` 无鉴权可达（仅回退内置模型名，不泄漏号池） | ⭕ 可选加固见下 |

**可选加固：彻底关掉内嵌 `/gw`。**
`admin/server.py:166-173` 无条件挂载。想关掉有两种最小改法（都不影响 `/v1/*`）：

- 改 `server.py:166` 的判断条件，加一个环境变量开关，例如
  `if _CONVERTER_EMBEDDED and os.getenv("ADMIN_EMBED_GW", "1") != "0":`，然后部署时设 `ADMIN_EMBED_GW=0`；
- 或者在 `Dockerfile.admin` 里不 COPY `converter.py` —— **不行**，§1 第 1 条说明了
  `admin/backend.py` 硬依赖它，去掉会导致启动失败。

---

## 9. 排障表

| 现象 | 原因 / 处理 |
| --- | --- |
| 容器反复重启，日志 `ModuleNotFoundError: No module named 'converter'` | 镜像里缺 `converter.py` 等文件，用 `Dockerfile.admin` 重新构建（别只 COPY `admin/`） |
| 启动日志 `Access denied` / `Unknown database` | `ADMIN_DATABASE_URL` 错。**优先怀疑密码特殊字符没 URL 编码**（§3） |
| 启动卡在 `Can't connect to MySQL` | 用了外网地址或地址写错。同区应用请用 **内网地址**；确认 MySQL 实例已 Running |
| `docker run` 能起来，Sealos 上一直 Pending/Running 但打不开 | 容器端口填错（必须 `8790`）；或应用监听在 `127.0.0.1` —— 本镜像 CMD 已是 `0.0.0.0` |
| 后台能开，`/v1/*` 全部 401 | Key 传错形式。支持 `X-API-Key: sk-xxx` 和 `Authorization: Bearer sk-xxx` 两种 |
| 刷余额 / 调用偶发失败、被上游拒绝 | `X-Device-Token` 缺失导致的风控（§1 第 4 条），云端无解，只能接受 |
| 定时任务被重复执行 | 实例数 / worker 不是 1，改回单实例（§1 第 3 条） |
| 想让 Sealos 上的号和本地后台共用 | 用后台「同步设置 → 同步到线上 / 从线上拉取」，或让两边都连同一个 MySQL |

---

## 10. 附：本次新增/改动的文件

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `Dockerfile.admin` | **新增** | admin 独立部署镜像定义；根目录原 `Dockerfile` 未改动 |
| `DEPLOY_SEALOS.md` | **新增** | 本文档 |
| 其余源码 | **未改动** | 本方案只做容器化，不需要改一行业务代码 |

> 注意：`admin/backend.py:168` 之外的 token 回写走的是 `tempfile.mktemp` 落盘临时文件
> （`backend.py:40`），容器 `/tmp` 可写即可，**不需要**挂载 auth 目录、**不需要**持久卷。
