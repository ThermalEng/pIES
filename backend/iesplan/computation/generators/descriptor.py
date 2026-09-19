"""GeneratorDescriptor：生成器公开能力描述(0.8 切片 A)。

字段覆盖 ``manual/developer-guide/zh-CN/modules/generators.md`` 中
"GeneratorDescriptor 至少声明"清单；只做能力查询与兼容校验，不实现
任何生成逻辑。跨越边界的容器字段在构造时冻结。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iesplan.computation.protocols import (
    check_provider_ref,
    freeze_json_value,
    thaw_json_value,
)

_ASSEMBLY_SCHEMA_VERSION_PATTERN = re.compile(r"^\d+\.\d+(\.\d+)?$")


def _check_assembly_schema_version(value: object) -> str:
    """校验支持的装配 schema 版本（手册：MAJOR/MINOR）。

    接受裸版本（``1.0``，可带 patch）或完整精确引用
    （``<稳定ID>@<语义化版本>``）；其他形态一律拒绝。
    """
    if isinstance(value, str) and "@" in value:
        return check_provider_ref(value, "assembly_schema_version")
    if isinstance(value, str) and _ASSEMBLY_SCHEMA_VERSION_PATTERN.match(value):
        return value
    raise ValueError(f"assembly_schema_version 非法: {value!r}")


@dataclass(frozen=True, slots=True)
class GeneratorDescriptor:
    """单个 GeneratorProvider 的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``；CalculationConfig 的
    ``generator`` 选择与本 ``ref`` 相等才表示选中本 provider，provider
    自身不再另存第二份引用。

    字段覆盖手册“至少声明”清单：``options_schema`` 声明选项 schema，
    默认语义由 schema 内各条目自带；``resource_limits`` 声明硬资源上界；
    ``unit_system`` 固定内部单位体系；``media_types`` 声明生成的输入
    媒体类型；``seed_semantics`` 说明种子语义。映射字段在构造时递归冻结。

    ``assembly_schema_version``、``solvers``、``result_adapter`` 必须为
    非空有效引用/版本：缺失即发布无效 descriptor，当场拒绝。
    """

    generator_id: str
    version: str
    assembly_schema_version: str = ""
    modes: tuple[str, ...] = ()
    precisions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    solvers: tuple[str, ...] = ()
    executors: tuple[str, ...] = ()
    equation_contracts: tuple[str, ...] = ()
    result_adapter: str = ""
    deterministic: bool = True
    options_schema: Mapping[str, object] = field(default_factory=dict)
    resource_limits: Mapping[str, object] = field(default_factory=dict)
    unit_system: str = ""
    media_types: tuple[str, ...] = ()
    seed_semantics: str = ""

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.generator_id}@{self.version}", "generator")
        _check_assembly_schema_version(self.assembly_schema_version)
        solvers = tuple(self.solvers)
        if not solvers:
            raise ValueError("solvers 不得为空：descriptor 必须声明兼容求解器")
        for solver_ref in solvers:
            check_provider_ref(solver_ref, "solvers")
        object.__setattr__(self, "solvers", solvers)
        for attr in (
            "capabilities",
            "executors",
            "equation_contracts",
            "media_types",
        ):
            items = tuple(getattr(self, attr))
            for name in items:
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{attr} 条目须为非空字符串: {name!r}")
            object.__setattr__(self, attr, items)
        modes = tuple(self.modes)
        if not modes or any(not isinstance(m, str) or not m for m in modes):
            raise ValueError("modes 须为非空能力集合")
        object.__setattr__(self, "modes", modes)
        precisions = tuple(self.precisions)
        if not precisions or any(p not in ("low", "medium", "high") for p in precisions):
            raise ValueError(
                "precisions 须为 low|medium|high 的非空子集: "
                f"{self.precisions!r}"
            )
        object.__setattr__(self, "precisions", precisions)
        if not self.result_adapter:
            raise ValueError(
                "result_adapter 不得为空：descriptor 必须声明配套结果适配器"
            )
        check_provider_ref(self.result_adapter, "result_adapter")
        if not isinstance(self.deterministic, bool):
            raise TypeError("deterministic 须为布尔值")
        for attr in ("options_schema", "resource_limits"):
            mapping = getattr(self, attr)
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{attr} 须为 Mapping")
            for key in mapping:
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{attr} 键须为非空字符串: {key!r}")
            object.__setattr__(self, attr, freeze_json_value(dict(mapping)))
        for attr in ("unit_system", "seed_semantics"):
            if not isinstance(getattr(self, attr), str) or not getattr(self, attr):
                raise ValueError(f"{attr} 须为非空字符串")

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
            "precisions": list(self.precisions),
            "capabilities": list(self.capabilities),
            "solvers": list(self.solvers),
            "executors": list(self.executors),
            "equation_contracts": list(self.equation_contracts),
            "result_adapter": self.result_adapter,
            "deterministic": self.deterministic,
            "options_schema": thaw_json_value(self.options_schema),
            "resource_limits": thaw_json_value(self.resource_limits),
            "unit_system": self.unit_system,
            "media_types": list(self.media_types),
            "seed_semantics": self.seed_semantics,
        }


__all__ = ["GeneratorDescriptor"]
