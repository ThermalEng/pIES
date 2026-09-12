"""输入格式正则(用户输入边界校验, 无 framework/ORM 依赖)。

幂等键格式的唯一权威: 任务提交 API/用例与 DDL 引用均改接本常量,
不再各自复述字面量。models 与 tasks 域均可导入本模块, 不形成循环。
"""

from __future__ import annotations

#: 幂等键(字母/数字/._:-, 1-128 位)。
IDEMPOTENCY_KEY_RE: str = "^[A-Za-z0-9._:-]{1,128}$"

__all__ = ["IDEMPOTENCY_KEY_RE"]
