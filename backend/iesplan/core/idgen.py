"""ID 生成与哈希工具。

- new_id:不可猜测随机 id,基于 secrets.token_urlsafe(CONTRACT 第 2 节)。
- new_idempotency_key:幂等键(短随机串,可加时间信息以便观测)。
- sha256_hex:字节数据 SHA-256 十六进制摘要。
"""

from __future__ import annotations

import hashlib
import secrets


def new_id(prefix: str = "") -> str:
    """生成不可猜测的随机 id。

    参数:
        prefix: 可选前缀(如 "prj-"/"tsk-"),返回 f"{prefix}{随机串}"。
    返回:
        随机 id 字符串(URL 安全字符,长度随前缀变化,默认 22 字符随机部分)。
    """
    token = secrets.token_urlsafe(16)
    return f"{prefix}{token}" if prefix else token


def new_idempotency_key() -> str:
    """生成幂等键。

    幂等键用于任务/写入去重:短随机串,便于在日志与监控中辨识。
    """
    return f"idem-{secrets.token_urlsafe(12)}"


def sha256_hex(data: bytes) -> str:
    """计算字节数据的 SHA-256 十六进制摘要。

    参数:
        data: 任意字节串。
    返回:
        64 位小写十六进制字符串。
    """
    return hashlib.sha256(data).hexdigest()
