"""公开命名空间生成与校验（任务书 §一）。

- 使用密码学安全随机源（secrets）生成 12 位小写 Crockford Base32 标识（60 bit 熵）；
- 数据库全局唯一，碰撞重试；
- 首次需要时分配，终身不变、不可转让、不复用；
- 不包含用户名、显示名、邮箱、数据库 ID 或其他个人信息。

Crockford Base32 字符集：0123456789ABCDEFGHJKMNPQRSTVWXYZ 的小写形式，
排除 I/L/O/U 混淆字符，32 字符 = 5 bit/字符，12 字符 = 60 bit。
"""

from __future__ import annotations

import re
import secrets

# Crockford Base32 小写字符集（32 字符 = 5 bit）
_CROCKFORD_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_CROCKFORD_SET = frozenset(_CROCKFORD_ALPHABET)

# 12 位 = 60 bit
NAMESPACE_LENGTH = 12

# slug 规则：小写字母/数字/连字符/下划线/点分段，单段不以分隔符开头结尾
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")
# slug 长度上限（与稳定 ID 组合后仍在合理范围）
SLUG_MAX_LENGTH = 64
SLUG_MIN_LENGTH = 1

# 完整稳定 ID 模式：user.<namespace>.device.<slug>
_STABLE_ID_RE = re.compile(
    rf"^user\.([{_CROCKFORD_ALPHABET}]{{12}})\.device\.([a-z0-9]+([._-][a-z0-9]+)*)$"
)


def is_valid_namespace(value: str) -> bool:
    """是否为合法的 12 位小写 Crockford Base32 命名空间。"""
    return (
        isinstance(value, str)
        and len(value) == NAMESPACE_LENGTH
        and all(ch in _CROCKFORD_SET for ch in value)
    )


def is_valid_slug(value: str) -> bool:
    """是否为合法的 slug（严格格式与长度）。"""
    return (
        isinstance(value, str)
        and SLUG_MIN_LENGTH <= len(value) <= SLUG_MAX_LENGTH
        and bool(_SLUG_PATTERN.fullmatch(value))
    )


def generate_namespace() -> str:
    """使用 CSPRNG 生成 12 位小写 Crockford Base32 标识（60 bit 熵）。"""
    # 每字符 5 bit，12 字符 = 60 bit；用 secrets.choice 保证密码学安全
    return "".join(secrets.choice(_CROCKFORD_ALPHABET) for _ in range(NAMESPACE_LENGTH))


def build_stable_id(namespace: str, slug: str) -> str:
    """组合稳定 ID：user.<namespace>.device.<slug>。"""
    return f"user.{namespace}.device.{slug}"


def parse_stable_id(stable_id: str) -> tuple[str, str] | None:
    """解析稳定 ID 为 (namespace, slug)，非法返回 None。"""
    m = _STABLE_ID_RE.fullmatch(stable_id)
    if not m:
        return None
    return m.group(1), m.group(2)


def is_valid_stable_id(value: str) -> bool:
    """是否为合法的 user.<namespace>.device.<slug> 稳定 ID。"""
    return parse_stable_id(value) is not None
