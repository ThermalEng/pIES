"""GeneratorDescriptor：生成器公开能力描述(0.8 切片 A)。

字段覆盖 ``manual/developer-guide/zh-CN/modules/generators.md`` 中
"GeneratorDescriptor 至少声明"清单；只做能力查询与兼容校验，不实现
任何生成逻辑。跨越边界的容器字段在构造时冻结。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iesplan.computation.protocols import (
    check_provider_ref,
    freeze_json_value,
    thaw_json_value,
)

def _check_options_schema(schema: object) -> Mapping[str, object]:
    """校验 options_schema 为完整 Draft 2020-12 JSON Schema 对象。

    根 schema 必须描述 object，且 ``additionalProperties`` 必须显式为
    ``false``（未知字段拒绝）；旧 properties 片段、空 ``{}`` 与非法
    schema 一律拒绝。返回冻结后的 schema 映射。
    """
    if not isinstance(schema, Mapping):
        raise TypeError(
            f"options_schema 须为完整 JSON Schema 对象，得到 {type(schema).__name__}"
        )
    if schema.get("type") != "object":
        raise ValueError("options_schema 根 schema 必须描述 object")
    if schema.get("additionalProperties") is not False:
        raise ValueError("options_schema 必须显式声明 additionalProperties: false")
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise ValueError(f"options_schema 自身非法: {exc}") from exc
    return freeze_json_value(dict(schema))


def _check_assembly_schema_version(value: object) -> str:
    """只声明当前装配契约，不接受版本范围或引用别名。"""
    if value == "1.0.0":
        return value
    raise ValueError(f"assembly_schema_version 非法: {value!r}")


@dataclass(frozen=True, slots=True)
class GeneratorDescriptor:
    """单个 GeneratorProvider 的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``；CalculationConfig 的
    ``generator`` 选择与本 ``ref`` 相等才表示选中本 provider，provider
    自身不再另存第二份引用。

    字段覆盖手册“至少声明”清单：``options_schema`` 必须为完整
    Draft 2020-12 JSON Schema 对象（根描述 object，
    ``additionalProperties`` 显式 false；无选项时调用方显式提交完整空
    对象 schema，不用空 ``{}``）；``resource_limits`` 声明硬资源上界；
    ``unit_system`` 固定内部单位体系；``media_types`` 声明生成的输入
    媒体类型；``seed_semantics`` 说明种子语义。映射字段在构造时递归冻结。
    schema 内 ``default`` 只作描述，不据此修改配置。

    ``assembly_schema_version``、``solvers``、``result_adapter`` 必须为
    非空有效引用/版本：缺失即发布无效 descriptor，当场拒绝。
    """

    generator_id: str
    version: str
    options_schema: Mapping[str, object]
    outputs: tuple[str, ...]
    assembly_schema_version: str = ""
    modes: tuple[str, ...] = ()
    precisions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    solvers: tuple[str, ...] = ()
    executors: tuple[str, ...] = ()
    equation_contracts: tuple[str, ...] = ()
    result_adapter: str = ""
    deterministic: bool = True
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
            "outputs",
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
        if not self.outputs:
            raise ValueError("outputs 须为非空能力集合")
        if not self.result_adapter:
            raise ValueError(
                "result_adapter 不得为空：descriptor 必须声明配套结果适配器"
            )
        check_provider_ref(self.result_adapter, "result_adapter")
        if not isinstance(self.deterministic, bool):
            raise TypeError("deterministic 须为布尔值")
        object.__setattr__(self, "options_schema", _check_options_schema(self.options_schema))
        resource_limits = self.resource_limits
        if not isinstance(resource_limits, Mapping):
            raise TypeError("resource_limits 须为 Mapping")
        for key in resource_limits:
            if not isinstance(key, str) or not key:
                raise ValueError(f"resource_limits 键须为非空字符串: {key!r}")
        object.__setattr__(self, "resource_limits", freeze_json_value(dict(resource_limits)))
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
            "outputs": list(self.outputs),
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
