"""pIES API 路由包 (空包标记)。

本阶段仅导出包版本; 健康检查路由暂由 iesplan.main 内联实现,
业务路由 (auth/projects/plans 等) 在后续阶段实现并在此包挂载。
"""

from iesplan import __version__ as __version__

__all__ = ["__version__"]
