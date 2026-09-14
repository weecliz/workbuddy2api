# 配置说明（.env）

本项目所有配置（数据库、后台密码、JWT 密钥、上游地址等）**不再写死在代码里**，统一通过环境变量读取，推荐放在项目根目录的 `.env` 文件中。

## 快速开始

```bash
# 1. 复制模板
cp .env.example .env

# 2. 编辑 .env，填入真实值（尤其是带 * 的敏感项）
#    Windows PowerShell:
#    copy .env.example .env

# 3. 安装依赖（含 python-dotenv）
pip install -r requirements.txt

# 4. 正常启动
python main.py
```

`config.py` 会在启动时调用 `load_dotenv()` 自动加载根目录的 `.env`，无需手动 export。

## 重要约定

- **`.env` 已被 `.gitignore` 忽略，绝不会提交到 git。** 请妥善保管，不要外泄。
- 提交到仓库的只有 `.env.example`（占位模板，无真实密钥）。
- 生产 / 开发 / CI 各环境使用**各自独立**的 `.env`，互不覆盖。
- 若系统已通过环境变量（systemd / 容器 / `export`）注入了同名变量，`.env` 中的值不会覆盖它们——便于在部署平台直接注入密钥。

## 生成强 JWT 密钥

```bash
python -c "import secrets;print(secrets.token_hex(32))"
```

把输出填入 `.env` 的 `ADMIN_JWT_SECRET`。

## 变量清单

### 数据库（类型 + 连接参数）

数据库配置统一由 `admin/db_config.py` 解析：既支持 `ADMIN_DB_TYPE` + 分项参数，
也支持旧的 `ADMIN_DATABASE_URL` 显式连接串（优先级更高）。详见 [DB_SUPPORT.md](DB_SUPPORT.md)。

| 变量 | 说明 | 生产必填 |
| --- | --- | --- |
| `ADMIN_DB_TYPE` | 数据库类型：`mysql` \| `db2` \| `sqlite` | |
| `ADMIN_DB_HOST` / `ADMIN_DB_PORT` | 地址 / 端口（留空用该类型默认值；SQLite 忽略） | |
| `ADMIN_DB_USER` / `ADMIN_DB_PASSWORD` | 账号 / 密码（SQLite 忽略） | ✅ |
| `ADMIN_DB_NAME` | 库名（DB2 为已存在的数据库名）；**SQLite 时为数据文件路径** | ✅ |
| `ADMIN_DB_SCHEMA` | DB2 专用目标 schema；MySQL / SQLite 忽略 | |
| `ADMIN_DB_OPTIONS` | 方言原生参数串，如 `charset=utf8mb4`（SQLite 忽略） | |
| `ADMIN_DATABASE_URL` | 显式连接串（可选，优先级最高） | |
| `ADMIN_DB_AUTO_CREATE` | `0` = 启动时不自动建库 / 建 schema / 建数据文件目录 | |
| `ADMIN_DB_POOL_*` / `ADMIN_DB_CONNECT_TIMEOUT` / `ADMIN_DB_ECHO` | 连接池与调试开关 | |

自检当前生效配置（密码会打码）：`python -m admin.db_config`

### 其余变量

| 变量 | 说明 | 生产必填 |
| --- | --- | --- |
| `ADMIN_REDIS_URL` | Redis 连接串 | |
| `ADMIN_BACKEND` | 上游网关地址 | |
| `ADMIN_CLIENT_AUTH_DIR` | 桌面端登录态目录 | |
| `ADMIN_USERNAME` | 后台管理员账号 | |
| `ADMIN_PASSWORD` | 后台登录密码 | ✅ |
| `ADMIN_JWT_SECRET` | JWT 签名密钥（>=32 字节随机串） | ✅ |
| `ADMIN_JWT_EXPIRE_HOURS` | Token 有效期（小时） | |
| `ADMIN_HOST` / `ADMIN_PORT` | 监听地址 / 端口 | |
| `ADMIN_COST_PER_TOKEN` | 积分估算系数（每千 token） | |
| `ADMIN_UPSTREAM_CLIENT_HEADER` | 上游客户端 IP 头名 | |
| `ADMIN_UPSTREAM_CLIENT_NAME` | 上游 client 产品名 | |
| `ADMIN_ACCOUNT_SELECT` | 账号选择策略 | |
| `ADMIN_LOGIN_MAX_ATTEMPTS` 等 | 登录防爆破参数 | |
| `ADMIN_CORS_ORIGINS` | CORS 允许来源 | |

完整说明见 `.env.example` 内注释。
