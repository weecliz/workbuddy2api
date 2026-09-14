"""管理后台配置（FastAPI + MySQL/DB2 + Redis）。

所有项均从环境变量读取，推荐通过项目根目录的 `.env` 文件提供（不要写死在代码里）。
复制 `.env.example` 为 `.env` 并填入实际值后使用：

    cp .env.example .env
    # 然后编辑 .env 填入真实数据库密码 / 后台密码 / JWT 密钥等

本地开发默认值只是占位，生产环境务必在 `.env` 中覆盖敏感项。
"""
import os
import logging

from dotenv import load_dotenv

from admin.db_config import db_config

# 加载项目根目录的 .env（无论运行时 CWD 在哪都能找到）。
# 不会覆盖已经存在的系统环境变量（便于容器 / systemd 注入）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

_logger = logging.getLogger(__name__)


class Settings:
    # 数据库 / 缓存
    # 数据库不再在这里写死：类型与连接参数统一由 admin/db_config.py 解析
    # （既支持 ADMIN_DATABASE_URL 显式连接串，也支持 ADMIN_DB_TYPE + 分项参数），
    # 这里只做一处透出，业务代码仍像以前一样用 settings.DATABASE_URL。
    DB_CONFIG = db_config
    DB_TYPE = db_config.type          # mysql | db2
    DATABASE_URL = db_config.url
    REDIS_URL = os.getenv("ADMIN_REDIS_URL", "redis://127.0.0.1:6379/0")

    # 后端（CodeBuddy / WorkBuddy）
    BACKEND = os.getenv("ADMIN_BACKEND", "https://copilot.tencent.com")

    # 本机 WorkBuddy/CodeBuddy 桌面端登录态目录（用于「扫描本机 / 注入切换」）
    CLIENT_AUTH_DIR = os.getenv(
        "ADMIN_CLIENT_AUTH_DIR",
        r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth",
    )

    # 管理后台登录
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    # 生产环境务必在 .env 中设置 ADMIN_JWT_SECRET 为 >=32 字节的随机串；
    # 未设置时回退到开发弱密钥并输出告警。
    _jwt = os.getenv("ADMIN_JWT_SECRET")
    if not _jwt:
        _logger.warning("ADMIN_JWT_SECRET 未设置，使用开发弱密钥；生产环境请在 .env 中配置")
        _jwt = "dev-insecure-jwt-secret-change-me"
    JWT_SECRET = _jwt
    JWT_EXPIRE_HOURS = int(os.getenv("ADMIN_JWT_EXPIRE_HOURS", "24"))

    # 服务监听
    HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
    PORT = int(os.getenv("ADMIN_PORT", "8790"))

    # /v1/messages（Anthropic Messages API）的模型档次映射。
    # Claude Code 发的是 claude-opus-* / claude-sonnet-* / claude-haiku-* 这类名字，
    # 上游不认；这里按档次落到本后台白名单里的模型名。
    # 注意：不要默认成 auto —— 本后台的 auto 是「取第一个启用的模型」，
    # 在 28 个模型里可能挑到 hunyuan-chat 之类不适合写代码的，甚至图像模型。
    # 下面三个默认值按「最强 / 均衡 / 快速」选，可按自己号池的实际情况改。
    ANTHROPIC_MODEL_OPUS = os.getenv("ADMIN_ANTHROPIC_MODEL_OPUS", "deepseek-v4-pro")
    ANTHROPIC_MODEL_SONNET = os.getenv("ADMIN_ANTHROPIC_MODEL_SONNET", "glm-5.2")
    ANTHROPIC_MODEL_HAIKU = os.getenv("ADMIN_ANTHROPIC_MODEL_HAIKU", "glm-5.3-flash")

    # /v1/messages 的 harness 脱敏开关（与 converter 的 /gw 端点同款处理）。
    # Claude Code 的 system prompt / tools 是固定模板，内含 "DoS / exploit / credential"
    # 这类合规声明词，会被上游内容审核误判并整条拒绝，典型报错就是
    #   400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}
    # 开启后会压缩 harness 并给敏感词插零宽空格。默认开启 —— 关掉大概率直接发不出去。
    ANTHROPIC_DESENSITIZE = os.getenv("ADMIN_ANTHROPIC_DESENSITIZE", "1") != "0"
    # 配合上项：跳过 harness 压缩，只做零宽脱敏（保留 system 原文，误拦风险略高）
    ANTHROPIC_NO_COMPACT = os.getenv("ADMIN_ANTHROPIC_NO_COMPACT", "0") == "1"

    # 给 OpenAI 协议的两个端点（/v1/chat/completions 与 /v1/responses）也启用同一套
    # harness 脱敏。默认**关闭**：普通 OpenAI 客户端（Cherry Studio / LobeChat 等）的
    # system prompt 很短，脱敏只会无谓改动 prompt；只有用 OpenAI 协议接「长 harness 客户端」
    # （如 Pi、claude-code-router 之类）时才需要打开，否则同样会撞 11128。
    OPENAI_DESENSITIZE = os.getenv("ADMIN_OPENAI_DESENSITIZE", "0") == "1"

    # 计费：后端未回传 credits 时，按 total_tokens * 系数 / 1000 估算（系数单位为「每千 token 积分」）
    COST_PER_TOKEN = float(os.getenv("ADMIN_COST_PER_TOKEN", "0.01"))

    # 上游记录客户端 IP 时使用的 header 名（若上游有自定义要求，如 X-Client-Ip / X-Real-IP 等）
    # 为空则同时发送 X-Forwarded-For / X-Real-IP / X-Client-IP 等常见头
    UPSTREAM_CLIENT_HEADER = os.getenv("ADMIN_UPSTREAM_CLIENT_HEADER", "")

    # 上游请求用量「client」列显示的产品名。
    # 腾讯 CodeBuddy/WorkBuddy 后端通过 X-IDE-Name 头识别客户端，默认 "WorkBuddy"。
    UPSTREAM_CLIENT_NAME = os.getenv("ADMIN_UPSTREAM_CLIENT_NAME", "WorkBuddy")

    # 账号选择策略：remain（剩余最多优先）/ lru（最久未用优先）
    ACCOUNT_SELECT = os.getenv("ADMIN_ACCOUNT_SELECT", "remain")

    # 登录防爆破：同一 IP 在窗口内失败超过阈值即锁定一段时间
    LOGIN_MAX_ATTEMPTS = int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "300"))  # 5 分钟窗口
    LOGIN_LOCK_SECONDS = int(os.getenv("ADMIN_LOGIN_LOCK_SECONDS", "900"))      # 锁 15 分钟

    # CORS：共享网关用 Authorization 头鉴权（无需 Cookie），故默认关闭 credentials；
    # 留空或 * 表示允许任意来源；如需限制可设 ADMIN_CORS_ORIGINS=https://a.com,https://b.com
    _raw_cors = os.getenv("ADMIN_CORS_ORIGINS", "*")
    CORS_ORIGINS = [o.strip() for o in _raw_cors.split(",") if o.strip()] or ["*"]


settings = Settings()
