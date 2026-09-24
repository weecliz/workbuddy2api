"""Anthropic 模型名 → 上游模型名的映射规则（admin / converter 两侧共用）。

Claude Code / CC Switch 发来的是 `claude-opus-4-6` 这类上游不认识的名字，需要按
配置翻译。规则全部来自 `.env`：

    ADMIN_ANTHROPIC_MODEL_MAP    精确映射表，形如
                                 "claude-opus-4-6=glm-5.3,claude-sonnet-4-5=deepseek-v4.1-flash"
    ADMIN_ANTHROPIC_MODEL_OPUS   档次兜底：名字含 opus 时用哪个上游模型
    ADMIN_ANTHROPIC_MODEL_SONNET 同上，sonnet
    ADMIN_ANTHROPIC_MODEL_HAIKU  同上，haiku

两侧判定顺序一致，差别只在第 3 步：

    1. 空 / "auto"                    → auto
    2. 精确映射命中                    → 映射目标
    3. 已在模型白名单（仅 admin 有白名单概念） → 原样透传
    4. 名字含 opus / sonnet / haiku    → 对应档次的目标模型
    5. 其余                           → admin 取 auto；converter 原样透传

本模块只做纯字符串规则：不查数据库、不读 settings，便于两侧复用与单独测试。
"""
import os

# 判定档次的关键词与顺序（opus 优先于 sonnet 优先于 haiku，与改动前一致）
TIER_NAMES = ("opus", "sonnet", "haiku")

ENV_MODEL_MAP = "ADMIN_ANTHROPIC_MODEL_MAP"
ENV_TIER_TEMPLATE = "ADMIN_ANTHROPIC_MODEL_{}"

# Claude Code 的 1M 上下文标记（如 `glm-5.3[1m]`）。它在客户端就被剥离并转成
# beta header，正常不会到达服务端；但用户手写映射表时很容易照抄这个写法，
# 所以解析与查表都统一按「去掉该后缀」的形态处理，避免静默失配。
_CONTEXT_SUFFIX = "[1m]"


def normalize_lookup_key(name: str) -> str:
    """查表用的规范化键：去首尾空白、转小写、剥掉末尾的 `[1m]` 标记。"""
    key = (name or "").strip().lower()
    if key.endswith(_CONTEXT_SUFFIX):
        key = key[: -len(_CONTEXT_SUFFIX)].strip()
    return key


def parse_model_map(raw: str | None) -> tuple[dict[str, str], list[str]]:
    """解析精确映射表，返回 `(映射, 非法条目)`。

    格式：`来源名=目标模型名`，多条用**英文逗号**分隔。来源名大小写不敏感，
    也接受带 `[1m]` 的写法（会被规范化掉）。

    非法条目（缺 `=`、来源或目标为空）不抛异常，只收集起来交给调用方告警——
    一个手滑的逗号不该让整个服务起不来。
    """
    mapping: dict[str, str] = {}
    invalid: list[str] = []
    for item in (raw or "").split(","):
        entry = item.strip()
        if not entry:
            continue
        source, sep, target = entry.partition("=")
        source, target = normalize_lookup_key(source), target.strip()
        if not sep or not source or not target:
            invalid.append(entry)
            continue
        mapping[source] = target
    return mapping, invalid


def lookup_exact(model: str, mapping: dict[str, str] | None) -> str | None:
    """精确映射查表；未命中返回 None。"""
    key = normalize_lookup_key(model)
    if not key or not mapping:
        return None
    return mapping.get(key)


def match_tier(model: str, tiers: dict[str, str] | None) -> str | None:
    """按 opus / sonnet / haiku 关键词匹配档次，返回目标模型；未命中返回 None。"""
    low = (model or "").strip().lower()
    if not low or not tiers:
        return None
    for tier in TIER_NAMES:
        target = (tiers.get(tier) or "").strip()
        if target and tier in low:
            return target
    return None


def load_tiers_from_env(getenv=os.getenv) -> dict[str, str]:
    """从环境变量读三档目标模型；未设置的档位为空串（即不参与匹配）。"""
    return {
        tier: (getenv(ENV_TIER_TEMPLATE.format(tier.upper())) or "").strip()
        for tier in TIER_NAMES
    }


def describe_invalid_entries(invalid: list[str]) -> str:
    """把非法条目拼成一句告警文案（调用方决定用 logging 还是 stderr）。"""
    return "ADMIN_ANTHROPIC_MODEL_MAP 有 %d 条无法解析（应为 来源名=目标模型名）：%s" % (
        len(invalid), ", ".join(repr(x) for x in invalid))
