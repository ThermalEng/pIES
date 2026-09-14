"""计算不可用错误(0.8 真实边界, 无算法实现)。"""

from __future__ import annotations


class ComputationUnavailableError(RuntimeError):
    """无可用计算能力时的真实边界错误。

    unavailable 语义(明确约定, 非防御性猜测):

    - 0.8 求解器/生成器未实现, provider 目录为空, 任何实际计算
      请求都必须以本错误明确失败;
    - 禁止调用方静默回退到默认结果、禁止猜测性成功;
    - ``reason`` 只取真实边界值: ``"no-provider"``(无可用 provider)
      或 ``"deferred-0.8"``(能力明确延期未实现)。

    本异常只表达边界事实, 不携带算法逻辑。
    """

    def __init__(
        self,
        message: str = "计算能力不可用(0.8 未实现, 无可用 provider)",
        *,
        reason: str = "no-provider",
    ) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = ["ComputationUnavailableError"]
