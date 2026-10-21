"""ResultAdapterProvider 真实实现(0.10 切片 B：首个 HiGHS 适配器)。

公开 ``result_adapters.highs`` 首个真实 provider；原子注册表见
``iesplan.computation.result_adapter``。provider 选择与装配只归
bootstrap 组合根；API/Worker 不得自行发现或动态加载 provider。
"""

from iesplan.computation.result_adapters.highs import (
    HIGHS_ADAPTER,
    HighsResultAdapter,
)

__all__ = [
    "HIGHS_ADAPTER",
    "HighsResultAdapter",
]
