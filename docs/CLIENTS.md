# 客户端接入

> 本文是 README 的细节拆分：各客户端（Codex CLI / Claude Code / OpenAI 系）的完整接入配置。

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

