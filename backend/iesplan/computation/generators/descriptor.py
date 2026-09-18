"""GeneratorDescriptor：生成器公开能力描述(0.8 切片 A)。

字段覆盖 ``manual/developer-guide/zh-CN/modules/generators.md`` 中
"GeneratorDescriptor 至少声明"清单；只做能力查询与兼容校验，不实现
任何生成逻辑。跨越边界的容器字段在构造时冻结。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.computation.protocols import check_provider_ref


@dataclass(frozen=True, slots=True)
class GeneratorDescriptor:
    """单个 GeneratorProvider 的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``；CalculationConfig 的
    ``generator`` 选择与本 ``ref`` 相等才表示选中本 provider，provider
    自身不再另存第二份引用。
    """

    generator_id: str
    version: str
    assembly_schema_version: str = ""
    modes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    solvers: tuple[str, ...] = ()
    executors: tuple[str, ...] = ()
    equation_contracts: tuple[str, ...] = ()
    result_adapter: str = ""
    deterministic: bool = True

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.generator_id}@{self.version}", "generator")
        if not isinstance(self.assembly_schema_version, str):
            raise TypeError("assembly_schema_version 须为字符串")
        for attr in ("modes", "capabilities", "solvers", "executors", "equation_contracts"):
            items = tuple(getattr(self, attr))
            for name in items:
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{attr} 条目须为非空字符串: {name!r}")
            object.__setattr__(self, attr, items)
        if self.result_adapter:
            check_provider_ref(self.result_adapter, "result_adapter")
        if not isinstance(self.deterministic, bool):
            raise TypeError("deterministic 须为布尔值")

    @property
    def ref(self) -> str:
        """生成器精确引用(``<稳定ID>@<语义化版本>``)。"""
        return f"{self.generator_id}@{self.version}"

    def supports_solver(self, solver_ref: str) -> bool:
        """该 provider 是否兼容给定 solver 精确引用。"""
        return solver_ref in self.solvers

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "generator_id": self.generator_id,
            "version": self.version,
            "assembly_schema_version": self.assembly_schema_version,
            "modes": list(self.modes),
            "capabilities": list(self.capabilities),
            "solvers": list(self.solvers),
            "executors": list(self.executors),
            "equation_contracts": list(self.equation_contracts),
            "result_adapter": self.result_adapter,
            "deterministic": self.deterministic,
        }


__all__ = ["GeneratorDescriptor"]
