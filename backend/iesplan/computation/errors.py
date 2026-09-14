"""计算能力尚未实现时的明确启动错误。"""

from __future__ import annotations


class ComputationUnavailableError(RuntimeError):
    """无可用计算能力时的真实边界错误。

    本异常只表达当前没有可启动的计算能力，不携带算法逻辑或回退行为。
    """

    def __init__(
        self,
        message: str = "计算能力不可用（0.8 尚未实现）",
        *,
        reason: str = "no-provider",
    ) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = ["ComputationUnavailableError"]
