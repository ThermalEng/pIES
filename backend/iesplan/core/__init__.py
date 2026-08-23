"""pIES 核心工具层。

包含单位换算、时间轴、诊断体系、异常、ID 生成、安全(密码/会话)与受控注册表。
设计约束见 manual/developer-guide/zh-CN/contracts.md 与 architecture.md。
"""

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.errors import error_envelope

__all__ = ["Diagnostic", "error_envelope", "make_diag"]
