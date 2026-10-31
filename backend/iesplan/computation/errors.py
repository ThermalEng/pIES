"""计算能力尚未实现时的明确启动错误。"""

from __future__ import annotations


class ComputationUnavailableError(RuntimeError):
    """无可用计算能力时的真实边界错误。

    表达不可用能力或当前模型无法完成转换；可携带已有关系来源，
    不执行算法逻辑或回退。
    """

    def __init__(
        self,
        message: str = "计算能力不可用",
        *,
        reason: str = "no-provider",
        model_source: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.model_source = model_source


__all__ = ["ComputationUnavailableError"]
