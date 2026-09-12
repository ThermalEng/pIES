"""输入格式正则(用户输入边界校验, 无 framework/ORM 依赖)。

幂等键格式的唯一权威: 任务提交 API/用例与 DDL 引用均改接本常量,
不再各自复述字面量。models 与 tasks 域均可导入本模块, 不形成循环。
"""

from __future__ import annotations

#: 幂等键(字母/数字/._:-, 1-128 位)。
IDEMPOTENCY_KEY_RE: str = "^[A-Za-z0-9._:-]{1,128}$"
#: 用户名(小写字母/数字/下划线, 3-32 位; 用户输入边界校验与 DDL 的唯一权威)。
USERNAME_RE: str = "^[a-z0-9_]{3,32}$"
#: 邮箱格式(用户输入边界校验与 DDL 的唯一权威)。
EMAIL_RE: str = r"^[^@\s]+@[^@\s]+$"

__all__ = ["IDEMPOTENCY_KEY_RE", "USERNAME_RE", "EMAIL_RE"]
