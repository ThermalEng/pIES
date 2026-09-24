"""执行 attempt 上下文：执行层消费的最小不可变身份。

只携带本次执行所需的四项身份：attempt、Bundle、装配与计算配置。
不含 Worker 租约、重试计数、数据库句柄或插件状态；取消与资源实施
随真实隔离能力落地，不在此预留字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """一次执行尝试的身份上下文（调用方 Worker/application 侧提供）。

    四项身份均为非空不透明字符串，执行层只负责把它们原样写入回执，
    不解释其业务含义，不读取装配或配置内容。
    """

    attempt_id: str = ""
    bundle_id: str = ""
    assembly_id: str = ""
    calculation_config_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "bundle_id",
            "assembly_id",
            "calculation_config_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"attempt 上下文缺必需身份: {name}")

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "attempt_id": self.attempt_id,
            "bundle_id": self.bundle_id,
            "assembly_id": self.assembly_id,
            "calculation_config_id": self.calculation_config_id,
        }


__all__ = ["AttemptContext"]
