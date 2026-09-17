"""设备指纹稳定派生（derive_id）。

用途：给全部上游请求注入稳定的伪物理设备特征，让同一账号长期表现为
「总是来自同一台设备」，同时让不同账号之间彼此隔离，降低多号关联风控。

关键设计（三条，改动前务必先读）：

  1. **与账号绑定、与部署环境无关**：只依赖账号自身的 uid 与固定业务盐，
     是纯哈希。不查注册表、不读机器码、不依赖桌面端或 Turing SDK ——
     因此容器 / 云端（拿不到 `X-Device-Token` 的场景）同样算得出来。
  2. **幂等**：同一 uid + 同一 salt 永远得到同一值。绝不能引入随机数或时间戳
     （除 `generate_request_id` 的尾部序号，那是每请求唯一、前缀仍稳定）。
  3. **算法口径与参考实现一致**：`md5("<salt>:<uid>")[:36]`。
     该口径来自参考实现 workbuddy2api-hub 的 wb_fingerprint.py，此处**逐字对齐**
     （包括 md5 只有 32 位十六进制、`[:36]` 实为取值不足的冗余切片这一点），
     目的是换端/混用同一号池时派生出**完全相同**的设备标识，
     避免同一账号在两套程序下呈现两套设备指纹而被判定为异常登录。

注意：本模块只负责「稳定」；是否启用、以及注入哪些头，由
`core.converter._build_headers_from` 与 `ADMIN_DEVICE_FINGERPRINT` 决定。
"""
import hashlib
import time


def derive_id(uid: str, salt: str) -> str:
    """由 uid + salt 稳定派生一个设备/会话标识。

    幂等：同一账号每次调用结果相同。uid 缺失时退化为固定串 "anonymous"
    （与参考实现一致），保证函数永不抛错、永不返回空。
    """
    seed = f"{salt}:{uid or 'anonymous'}"
    # 与参考实现逐字对齐：md5 十六进制为 32 字符，[:36] 不改变结果，
    # 但保留它可以确保未来若上游口径变化时两套实现仍然一致。
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]


def generate_request_id(uid: str) -> str:
    """生成带稳定前缀与微秒级尾部序号的 X-Request-ID。

    前缀取自 uid（同账号稳定），尾部为单调变化的 6 位序号，
    因此每条请求唯一、但同一账号的前缀可被识别为同一设备来源。
    """
    prefix = derive_id(uid, "req")
    suffix = str(time.time_ns() % 1000000).zfill(6)
    return f"{prefix}-{suffix}"


def device_headers(uid: str) -> dict:
    """本项目要注入的三个设备头（machineId / sessionId / requestId）。

    收敛成一处，避免调用方各自拼 key 拼错；converter 只做一次 dict update。
    """
    return {
        "X-Machine-ID": derive_id(uid, "machine"),
        "X-Session-ID": derive_id(uid, "session"),
        "X-Request-ID": generate_request_id(uid),
    }
