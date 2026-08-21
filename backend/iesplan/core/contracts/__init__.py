"""core/contracts: 无状态纯类型(宪法 4.1)。

- ``ParameterSpec`` 等公共数据类型归属本包, 不携带注册状态;
- core 包禁止导入任何业务模块。
"""

from iesplan.core.contracts.parameters import ParameterSpec

__all__ = ["ParameterSpec"]
