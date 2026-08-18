"""IES Plan 核心工具层。

包含单位换算、时间轴、诊断体系、异常、ID 生成、安全(密码/会话)与受控注册表。
实现依据:docs/CONTRACT.md 第 2 节、docs/spec/02-calc-model.md、docs/spec/04-registry-diagnostics.md。
"""

from iesplan.core.diagnostics import Diagnostic, make_diag

__all__ = ["Diagnostic", "make_diag"]
