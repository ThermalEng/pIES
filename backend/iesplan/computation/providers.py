"""计算 provider 目录(0.8 真实边界: 为空, 明确无可用 provider)。

组合根(``iesplan.bootstrap``)经本模块取目录, 不再自建空 dict;
目录仍为空, unavailable 语义不变。
"""

from __future__ import annotations

from typing import Any


def available_providers() -> dict[str, Any]:
    """返回当前可用的 computation provider 目录。

    0.8 未实现: 恒为空 dict(调用方持有副本, 禁止就地改写注册)。
    """
    return {}


__all__ = ["available_providers"]
