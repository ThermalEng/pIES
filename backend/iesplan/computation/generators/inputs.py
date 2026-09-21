"""生成输入边界(0.8f-A)：调用方固定后交给 provider 的不可变内容。

``GenerationInputs`` 是纯 computation/generator 能力的调用边界：
调用方（后续 application 编排）从已签发装配产物、已固定 revision
的设备描述符、不可变财务快照与已编译规划 IR 构造本对象后，
provider 只消费本对象 + 已校验 ``CalculationConfig``，不再回读
数据库、对象存储、装配 YAML 或用户表达式。

只做类型/结构边界（键形态、值类型、排序冻结），不重新验证已签发
artifact、设备内容、财务快照或 CSV 序列，不做 hash/摘要。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from iesplan.core.contracts import PlanningProgram
from iesplan.finance import EffectiveFinanceConfig
from iesplan.modeling import DeviceMathContribution, contribution_to_dict

__all__ = ["GenerationInputs"]


def _frozen_sorted(mapping: Mapping[str, Any], *, what: str) -> Mapping[str, Any]:
    """按键排序冻结（确定性顺序；键须为字符串）。"""
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{what} 须为 Mapping，得到 {type(mapping).__name__}")
    for key in mapping:
        if not isinstance(key, str):
            raise TypeError(f"{what} 的键须为字符串，得到 {key!r}")
    return MappingProxyType({key: mapping[key] for key in sorted(mapping)})


@dataclass(frozen=True, slots=True)
class GenerationInputs:
    """Provider 生成输入（公开不可变契约）。

    属性:
        resources: 内容 ID → 只读字节（固定对象身份到内容；调用方
            已按对象 ID 确认存在，含 0.8e 物化的全周期连续序列）。
        contributions: 装配实例 ID → 设备数学贡献（键为 instance_id，
            不是 device type；由调用方按实例绑定构造）。
        effective_finance: 不可变有效财务快照（经济参数唯一来源）。
        planning_program: 已编译规划 IR（目标/连续变量/已启用约束）。
    """

    resources: Mapping[str, bytes]
    contributions: Mapping[str, DeviceMathContribution]
    effective_finance: EffectiveFinanceConfig
    planning_program: PlanningProgram

    def __post_init__(self) -> None:
        resources = _frozen_sorted(self.resources, what="resources")
        for key, value in resources.items():
            if not isinstance(value, bytes):
                raise TypeError(f"resources[{key!r}] 须为 bytes")
        contributions = _frozen_sorted(self.contributions, what="contributions")
        for key, value in contributions.items():
            if not isinstance(value, DeviceMathContribution):
                raise TypeError(f"contributions[{key!r}] 须为 DeviceMathContribution")
        if not isinstance(self.effective_finance, EffectiveFinanceConfig):
            raise TypeError("effective_finance 须为 EffectiveFinanceConfig")
        if not isinstance(self.planning_program, PlanningProgram):
            raise TypeError("planning_program 须为 PlanningProgram")
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "contributions", contributions)

    def to_dict(self) -> dict[str, Any]:
        """确定性序列化（资源字节为 hex；贡献沿用建模规范 dict）。"""
        return {
            "resources": {
                key: value.hex() for key, value in self.resources.items()
            },
            "contributions": {
                key: contribution_to_dict(value)
                for key, value in self.contributions.items()
            },
            "effective_finance": self.effective_finance.to_dict(),
            "planning_program": self.planning_program.to_dict(),
        }
