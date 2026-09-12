# 用 Windows 云服务器部署（保留 `X-Device-Token` 的路线）

> 适用场景：你不想折腾容器/DevBox，希望像在自己机器上一样双击 `.bat` 把服务跑起来。
> 本文给出可行性判断、机器选型、两条落地形态、完整步骤，以及与 Sealos 路线的取舍对照。
> 本文**不含任何源码改动**，全部是部署操作。

---

## 0. 结论

**可以，而且这是唯一能保住 `X-Device-Token` 的部署方式。**

`X-Device-Token` 由 WorkBuddy 桌面端的 **Turing Shield SDK 原生模块**生成，
而该模块是 **win32 / darwin 专有**（`turing-sdk/index.cjs` 显式判断 `process.platform`，
二进制是 `TuringShieldSDK.dll`）。Linux 容器里永远拿不到 —— 这也是 Sealos 路线最大的功能代价。

代价是三条：

| 代价 | 说明 |
| --- | --- |
| 💰 更贵 | Windows 实例含授权费，且 Windows Server 自身吃 1.5 GB+ 内存，**2 核 4 G 起步** |
| 🔧 要自己运维 | 系统补丁、MySQL、开机自启、RDP 安全，全归你 |
| 🔓 HTTPS 要自己配 | 国内要域名 + 备案才能上 80/443 拿证书；只给 IP 就只能裸 HTTP |

先给一个决策速查：

- 只要「**自己几台设备能用**」→ 方式 A（`converter.py`），15 分钟搞定，不需要 MySQL。
- 要「**发给别人多个 Key、按 Key 限额、看用量**」→ 方式 B（`main.py`），需要 MySQL。

---

## 1. 源码级依据：为什么 Windows 能拿到，Linux 不能

关键文件是 `turing_helper.js`（Node 脚本）和 `admin/turing_token.py`（Python 侧）。

### 1.1 它只需要「SDK 目录在磁盘上」，**不需要桌面端在运行**

`turing_helper.js:51-112` 做的事情只有两件：**遍历候选目录找 SDK**，然后 **`require(sdkDir)`**。

```js
// turing_helper.js:31-34  已知的 SDK 相对路径（相对于桌面端安装基目录）
const REL_SDK_PATHS = [
  "resources/app.asar.unpacked/native/turing-sdk",
  "resources/native/turing-sdk",
];
// turing_helper.js:136 直接加载原生桥接，没有检查桌面端进程是否在跑
turing = require(sdkDir);
```

找到之后 `configure(channelId, productName, productVersion)` → `fetchDeviceToken()`（`turing_helper.js:163-169`）。

**推论：把 SDK 目录整体拷到云服务器上就够了**，不必安装完整的桌面端，也不必让它常驻运行。

### 1.2 可以显式指定 SDK 目录，绕开自动发现

`turing_helper.js:56-65` 优先读环境变量：

```
WORKBUDDY_TURING_SDK_DIR     # 指向含 index.cjs / TuringShieldSDK.dll 的目录（或桌面端安装基目录）
```

自动发现的候选目录（`turing_helper.js:66-104`）包括 `%LOCALAPPDATA%\WorkBuddy`、
`%ProgramFiles%\WorkBuddy`、以及各盘根目录下的 `WorkBuddy` / `workbuddy`。
服务器上装的位置如果不在这些范围内，就显式设这个变量。

### 1.3 ⚠️ 但必须装 Node.js

`admin/turing_token.py:43-45`：

```python
def _node_bin() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")
```

`shutil.which` 返回 `None` 就直接降级（`turing_token.py:84-89`），连 SDK 都不去找。
所以 **Windows 云服务器上必须装 Node.js**（这一点和 `Dockerfile.admin` 里故意不装 Node 的设计正好相反）。

### 1.4 验证是否拿到 token

在项目根目录执行：

```cmd
node turing_helper.js
```

- 成功：stdout 输出一行 `{"token":"v3:AAAA..."}`
- 失败：stderr 会列出它搜过的所有候选目录，并提示你设 `WORKBUDDY_TURING_SDK_DIR`

Python 侧的调用有 25 秒超时（`turing_token.py:101-104`）、10 分钟正缓存 + 60 秒负缓存，
失败只 WARNING 一次，**不会影响主流程**（`get_headers()` 里可选地加这个头）。

---

## 2. 机器怎么买

### 2.1 规格建议

| 项 | 建议 | 原因 |
| --- | --- | --- |
| 规格 | **2 核 4 G** | Windows Server 自身占 1.5 GB+，2 核 2 G 跑 Python + MySQL 会非常紧 |
| 系统盘 | 40 GB 起（建议 60 GB） | Windows 系统 + 桌面端（若装）+ MySQL 数据 + 日志 |
| 镜像 | **Windows Server 2022 数据中心版（带桌面体验）** | 云厂商公共镜像默认就是带桌面体验的；若要装桌面端，必须有 GUI |
| 带宽 | 3–5 Mbps（或按流量计费） | 文本 API 转发流量很小，但流式 SSE 并发多时窄带宽会拖慢 |
| 地域 | **必须国内**（北上广等） | 要访问 `copilot.tencent.com`，海外节点延迟高，且容易被判为异常地区 |
| 产品 | 轻量应用服务器够用 | 不需要 CVM 的弹性能力；本项目单实例单进程 |

### 2.2 价格参考（腾讯云轻量，中国内地，含 Windows 授权）

| 配置 | 参考价 |
| --- | --- |
| 2 核 2 G / 40 GB / 2 Mbps / 100 GB 流量 | 约 35 元/月 |
| 2 核 4 G / 60 GB / 5 Mbps / 500 GB 流量 | 约 65 元/月 |
| 4 核 8 G / 120 GB / 10 Mbps | 约 210 元/月 |

年付通常有 85 折、新用户活动价更低。**以购买页实时报价为准**，这里的数字只用于判断量级。

对比 Sealos：0.5 核 1 G 就能跑 admin，按量计费通常每月十几元量级。**差价基本就是「买到 `X-Device-Token`」的价钱。**

---

## 3. 两种落地形态

### 方式 A：`converter.py` 独立跑（单人 / 自己几台设备）

- 端口 `8787`，只需要 Python，**不需要 MySQL / Redis**
- 需要桌面端登录态文件（`.info`）
- 额外支持 `/v1/responses`、`/v1/messages`（Anthropic）、`/v1/balance`
- 对外鉴权靠启动参数 `--api-key`

```cmd
start_converter.bat
start_converter.bat --port 9000 --api-key <你的强密钥>
```

`converter.py:463` 的 `_check_auth()` 逻辑是 `if not key: return` ——
**不传 `--api-key` 就是完全不鉴权**。对外暴露端口时**必须**带上它。

### 方式 B：`main.py` 一键拉起（多人共享号池，本项目核心用途）

- 端口 `8790`，`/admin` 管理大屏 + `/v1/*` 带 Key 配额与记账的托管网关
- **需要 MySQL 8**；Redis 可选（连不上自动降级为内存计数）
- 客户端 `base_url` = `http://<IP>:8790/v1`，鉴权 `X-API-Key: sk-xxx` 或 `Authorization: Bearer sk-xxx`

```cmd
start_admin.bat
start_admin.bat --port 8080
```

⚠️ **`/gw/*` 即使在方式 B 下也是死的。**
`admin/server.py:27` 是 `from converter import app` 的 **import 方式**挂载，
而 `CONFIG["cred"]` / `CONFIG["api_key"]` 只在 `converter.py main()` 里赋值（被 `if __name__ == "__main__"` 守着），
`main()` 从不执行 ⇒ `/gw/v1/chat|balance|messages|responses` 全部 503。
要用那三个协议，**必须另外单独起一个 `converter.py`（8787）**。

### 推荐组合（功能最全）

同一台服务器上并行跑两个进程：

| 进程 | 端口 | 作用 |
| --- | --- | --- |
| `start_admin.bat` | 8790 | 管理后台 + 号池网关 `/v1/chat/completions`（多 Key 配额） |
| `start_converter.bat` | 8787 | 带 `--api-key`，提供 `/v1/responses`、`/v1/messages`、`/v1/balance` |

两个互不冲突，各自独立鉴权。**但注意 8787 走的是服务器本机那份 `.info` 登录态，
和 8790 的 MySQL 号池是两套东西**，别混。

---

## 4. 完整步骤

### 第 1 步：买机器 + 配安全组

安全组**只放行必需的端口**：

| 端口 | 用途 | 建议 |
| --- | --- | --- |
| RDP（改过的端口） | 远程桌面 | **限制来源 IP 为你自己的出口 IP** |
| 8790 | admin + 网关 | 对 `0.0.0.0/0` 开放（要给外部客户端用） |
| 8787 | converter | 只给需要用 responses/messages 的人，最好也限来源 |

### 第 2 步：装 Python

装 **Python 3.10+**（3.12 / 3.13 均可），安装时勾选 **Add python.exe to PATH**。

> `start_admin.bat` 找解释器的顺序是：`CONVERTER_PYTHON` 环境变量 →
> `%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe` →
> 项目内 `.venv` / `venv` → `where python`。
> 云服务器上前两个都不存在，所以**系统 Python 必须装好依赖**（或者把路径塞进 `CONVERTER_PYTHON`）。

### 第 3 步：装 Node.js

**必须装** —— 见 §1.3。装 LTS 版并确保 `node -v` 可用。

### 第 4 步：拿项目 + 装依赖

```cmd
cd /d D:\
git clone <你的仓库地址> workbuddy2api
cd workbuddy2api
python -m pip install -r requirements.txt
```

### 第 5 步：准备登录态与 SDK（两种做法）

**做法 i（推荐，闭环最顺）：在服务器上装 WorkBuddy 桌面端并登录**

- 通过 RDP 打开浏览器下载桌面端 → 安装 → 登录（扫码 / 验证码都在 RDP 桌面里操作）
- 好处：`.info` 和 `turing-sdk` 自动出现在正确位置，**一行配置都不用写**
- 额外好处：后台「账号 → 扫描本机」可直接把这份登录态收进号池
  （`admin/routers/accounts.py:147` 的 `scan-local`，读取 `settings.CLIENT_AUTH_DIR`，
  默认 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth`）

**做法 ii（不装桌面端，只拷两样东西）**

1. 从本机拷登录态文件到服务器同路径：

   ```
   本机： C:\Users\<你>\%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
   服务器：C:\Users\<你>\%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
   ```

   （`converter.py:106-121` 的 `auth_dirs()` 在 Windows 上就认这个目录；
   也可以用环境变量 `CODEBUDDY_AUTH_DIR` 指到别处。）

2. 从本机拷 SDK 目录过去，例如放 `D:\turing-sdk`，然后设系统环境变量：

   ```
   WORKBUDDY_TURING_SDK_DIR = D:\turing-sdk
   ```

   目录里应当有 `index.cjs` / `package.json` / `TuringShieldSDK.dll` / `*.node`。
   本机的 SDK 一般在 `D:\Program Files\WorkBuddy\resources\app.asar.unpacked\native\turing-sdk`。

3. 验证：

   ```cmd
   cd /d D:\workbuddy2api
   node turing_helper.js
   ```

   看到 `{"token":"v3:..."}` 就成了。

> ⚠️ 注意：这样生成的设备 token 绑定的是**服务器这台机器**的指纹，
> 上游看到的是「一台新设备」。如果你的账号同时在本机和服务器上用，会呈现为两台设备。

### 第 6 步：装 MySQL（只有方式 B 需要）

两条路：

- **本机装 MySQL 8 社区版**（项目已有在 Windows 上装 MySQL 8.4 的经验）
- **买云数据库 RDS**（更省心，但贵一些；注意安全组只放行你的服务器）

建 `.env`（复制 `.env.example` 再改），至少改这几项：

```ini
ADMIN_DATABASE_URL=mysql+pymysql://root:<密码>@127.0.0.1:3306/workbuddy_admin?charset=utf8mb4
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
ADMIN_JWT_SECRET=<32 字节以上随机串>
ADMIN_CORS_ORIGINS=*
```

> ⚠️ **密码里的特殊字符必须 URL 编码。** `admin/db.py:ensure_database()` 是用字符串劈分解析 URL 的
> （`rest.split("@", 1)` + `split("/")[-1]`），密码中的 `@` `/` `:` `#` 会让它错位。
> 生成随机密钥：`python -c "import secrets;print(secrets.token_urlsafe(48))"`
>
> 库不用手动建 —— 启动时 `ensure_database()` 会 `CREATE DATABASE IF NOT EXISTS`，
> 然后 `init_db()` 建 6 张表并做列迁移，两步都幂等。

### 第 7 步：启动

```cmd
start_admin.bat
```

看到这几行就成了：

```
[3/4] database    : reachable
管理后台   : http://127.0.0.1:8790/admin
托管网关   : http://127.0.0.1:8790/v1/chat/completions  (带 Key 配额)
```

需要 `responses` / `messages` 协议的话，再开一个窗口跑 `start_converter.bat`。

### 第 8 步：从外面验证

在**另一台电脑**上：

```cmd
curl http://<服务器公网IP>:8790/v1/models -H "Authorization: Bearer sk-xxxx"
```

浏览器打开 `http://<服务器公网IP>:8790/admin` 能登录即通。

---

## 5. 开机自启 + 崩溃自动重启

`.bat` 双击跑的问题是一关 RDP 会话就可能被杀掉、进程挂了不会自己起。用 **NSSM**（免费）注册成 Windows 服务最省心：

```cmd
REM 下载 nssm.exe 放到 C:\tools\
C:\tools\nssm.exe install workbuddy-admin "C:\Python313\python.exe" ^
  "-m uvicorn admin.server:app --host 0.0.0.0 --port 8790 --log-level info"
C:\tools\nssm.exe set workbuddy-admin AppDirectory D:\workbuddy2api
C:\tools\nssm.exe set workbuddy-admin AppStdout D:\workbuddy2api\logs\admin.out.log
C:\tools\nssm.exe set workbuddy-admin AppStderr D:\workbuddy2api\logs\admin.err.log
C:\tools\nssm.exe set workbuddy-admin AppExit Default Restart
C:\tools\nssm.exe set workbuddy-admin Start SERVICE_AUTO_START
C:\tools\nssm.exe start workbuddy-admin
```

converter 同理，参数换成：

```
"C:\Python313\python.exe" "converter.py --port 8787 --api-key <强密钥> --desensitize --log converter.log"
```

> `python.exe` 的实际路径用 `where python` 查。
> 不想装第三方工具的话，用「任务计划程序」也能做到：
> 触发器选「计算机启动时」，操作选启动程序，并勾选「不管用户是否登录都要运行」。

⏱ **两个进程都要在「Windows 防火墙 → 入站规则」里放行对应端口**，
安全组放行了只解决云厂商那一层。

---

## 6. 公网安全：RDP 是最大风险点

| 项 | 风险 | 动作 |
| --- | --- | --- |
| **RDP 3389 暴露公网** | 每天被爆破上千次，弱口令必沦陷 | **改端口 + 强密码 + 安全组限制来源 IP**。这是本题第一优先级 |
| `ADMIN_JWT_SECRET` 未设 | 回退到代码里公开的 `dev-insecure-jwt-secret-change-me`，代码开源 ⇒ 任何人可伪造管理员 token | **必设** |
| `ADMIN_PASSWORD` 默认 `admin123` | 启动时会告警但不会阻止 | **必改** |
| `ADMIN_CORS_ORIGINS` 默认 `*` | 任意网站 JS 都能带 Key 调你的网关 | 只自用可留 `*`；要严谨就填你自己的域名 |
| `/v1/*` 无限流 | `admin/ratelimit.py` **只保护 `/api/login`**，网关侧没有任何限流 | 每个 Key 的配额设小，定期看用量页 |
| 裸 HTTP 传 Key | 方式 B 默认无 TLS（`main.py:140` 起 uvicorn 没传 ssl 参数） | 见下 |
| converter 未带 `--api-key` | `_check_auth()` 是 `if not key: return`，**无鉴权** | 对外暴露时**必须**加 `--api-key` |

### 想上 HTTPS 的话

**路径 1：域名 + 备案 + Caddy（推荐，最省事）**

国内服务器要域名解析到 80/443，**必须 ICP 备案**。备案通过后装 Caddy：

```
# Caddyfile
your-domain.com {
    reverse_proxy 127.0.0.1:8790
}
```

Caddy 会自动申请并续期 Let's Encrypt 证书，不用手写 Nginx 配置。

**路径 2：只用 IP + 端口（免备案）**

- 客户端 `base_url` 填 `http://<IP>:8790/v1`，OpenAI SDK 是支持 http 的
- 代价：API Key 明文过公网，且部分客户端对非 https 有额外限制

**路径 3：Nginx 自签证书** —— 客户端要额外信任证书，麻烦，不推荐。

---

## 7. 两条路线怎么选

| 维度 | Windows 云服务器 | Sealos（Linux 容器） |
| --- | --- | --- |
| **`X-Device-Token`** | ✅ 可拿到（拷 SDK 目录 + 装 Node） | ❌ 永远拿不到（原生模块平台限制） |
| 「扫描本机 / 注入本机」 | ✅ 可用 | ❌ 失效（`CLIENT_AUTH_DIR` 是 Windows 路径） |
| converter 全协议（responses / messages / balance） | ✅ 单独跑 8787 即可 | ⚠️ 内嵌 `/gw` 是死代码 |
| 部署复杂度 | 低（双击 `.bat`，跟本机一样） | 中（要构建镜像 / 走 DevBox） |
| 成本 | 💰 65 元/月起 | 💰 十几元/月起 |
| HTTPS | 要域名 + 备案，或裸 HTTP | ✅ 自动签发 |
| 弹性 / 自愈 | ❌ 单机，挂了就没了 | ✅ K8s 托管 |
| 运维负担 | 系统补丁、RDP 安全、MySQL、自启全归你 | 平台托管 |

**一句话判断**：

- 只有**一个或少数几个账号、自己用** → Sealos / Linux 更划算，`X-Device-Token` 缺失的代价可以接受，先跑起来观察失败率。
- 要**多人共享号池**且**在意被风控**（尤其是「每日签到」这类敏感请求） → Windows 云服务器，问题最少。
- 想两边都要：**Windows 上跑 admin（号池网关）保功能，Sealos 上只跑一个静态展示页** —— 意义不大，不建议。

---

## 8. 排障表

| 现象 | 原因 / 处理 |
| --- | --- |
| `node turing_helper.js` 报「TuringShield SDK 未找到」 | 按它列出的候选目录核对；把 `WORKBUDDY_TURING_SDK_DIR` 指向含 `index.cjs` / `TuringShieldSDK.dll` 的目录 |
| 日志反复出现「X-Device-Token 不可用」 | `node` 不在 PATH（`shutil.which("node")` 返回 None）→ 装 Node.js，重启服务。**改完必须重启进程**，缓存不会自动刷新 |
| `start_admin.bat` 报「No usable Python interpreter found」 | 装 Python 勾选 Add to PATH，或设 `CONVERTER_PYTHON` 指向 `python.exe` |
| 报「This interpreter is missing runtime dependencies」 | `python -m pip install -r requirements.txt` |
| `[3/4] database : UNREACHABLE` | MySQL 没起，或 `.env` 里的地址/用户/密码错。**优先怀疑密码特殊字符没 URL 编码** |
| 本地能开 `/admin`，外网打不开 | 两层都要放行：云厂商安全组 + Windows 防火墙入站规则；确认 `main.py` 监听的是 `0.0.0.0` |
| `/gw/v1/...` 一直 503 | 已说明，是设计使然。要用就单独跑 `converter.py`（8787） |
| 远程桌面被爆破提示 | 立刻改 RDP 端口 + 换强密码 + 安全组限来源 IP |
| 定时任务（每日签到）重复执行 | 同一台机器上别重复启动多个 admin 进程；`admin/scheduler.py` 无跨进程锁 |

---

## 9. 附：几个容易忽略的代码细节

1. **`main.py` 的硬编码回退路径意外契合 Windows Server。**
   `main.py:121` 写死了 `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
   —— 在开发机上（用户 `weichao`）这个路径不存在所以没用，但 **Windows Server 默认管理员用户名就是 `Administrator`**，
   如果你按这个路径把依赖装好，回退分支反而会生效。不过更干净的做法还是直接用系统 Python 装依赖。

2. **`turing_token.py` 的失败是负缓存 60 秒，不是永久。**
   改完环境变量别急着看效果，但也不用等太久；最稳妥是重启进程（`shutil.which` 每次都会重新查）。

3. **`_client_ip` 依赖 `X-Forwarded-For`。**
   如果前面挂了 Caddy / Nginx 反代，记得让它透传 `X-Forwarded-For`，
   否则后台用量日志里的客户端 IP 会全是 `127.0.0.1`（`admin/routers/proxy.py:44`）。

4. **账号凭据是明文存 MySQL 的。**
   README §3.5 已声明。云服务器上别把 3306 开公网，也尽量别让别人拿到数据库账号。

5. **converter 和 admin 是两套账号体系。**
   admin 的号池在 MySQL（`accounts.auth_json`），converter 读的是本机 `.info` 文件。
   在服务器上装了桌面端的话，两者可以指向同一个账号，但**状态互不共享**。
