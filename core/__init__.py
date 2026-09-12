"""项目内核：上游协议适配与凭据管理（原根目录散落的库模块归拢于此）。

    converter.py            CredentialManager（凭据 / token 刷新 / 上游请求）
                            + /gw 单账号旁路的 FastAPI app
    responses_adapter.py    OpenAI Responses ↔ Chat 双向转换
    responses_projection.py Responses 请求体投影
    anthropic_adapter.py    Anthropic Messages ↔ Chat 双向转换
    desensitize.py          harness 脱敏（绕过上游内容审核误判）

入口脚本 main.py / service_admin.py 仍在项目根目录（它们要被 bat 与
Windows 服务直接调用），turing_helper.js 也留在根目录（admin/turing_token.py
按「项目根」约定引用它）。
"""
