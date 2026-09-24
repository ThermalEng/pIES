"""全周期已物化序列公共契约(0.8 切片 E)。

为 GeneratorProvider 服务的最小确定性物化逻辑，只处理非预测
来源（constant/data_repeat）；data_predict 声明式记入
``MaterializedSet.pending_predict``（保留固定资源身份），不物化、
不跳过、不伪装完成；预测训练与生成不属于物化阶段。

输入只消费 ``ValidatedAssemblyArtifact``（已签发事实）与调用方按
canonical 绑定 ``content.object_id``（字符串）显式传入的只读字节；
输出 step 恒为 ``0..point_count-1``，``point_count`` 由基线唯一推导
（复用 core 权威纯函数，不重建基线对象、不重复校验）。

信任装配已确认的事实，不做任何重复校验：不调用数据解析校验器，
不复核 schema/单位/范围/step/周期，不产生诊断，不做 hash/摘要。
CSV 读取只做取数必需的确定性解码与列投影（标准库 csv 读取，不校验、
不修复、不 fallback）；固定资源缺失明确失败；其余违反已签发事实的
内部损坏让实际 decode/csv/索引/float 错误可见。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.core.contracts import point_count_for_baseline


@dataclass(frozen=True, slots=True)
class MaterializedSeries:
    """单个预定义接口的全周期计算序列（不可变值对象）。

    ``values`` 按 step 0 起连续排列，长度恒等于 ``point_count``；
    ``source`` 固定追溯（constant 无外部引用，data_repeat/data_predict
    记 object_id）。``mode`` 为 constant、data_repeat、data_predict
    三者之一；物化只产出前两者，data_predict 完成值以同一类型进入
    （训练与生成不属于物化阶段）。
    """

    device: str
    interface: str
    mode: str
    resolution: str
    point_count: int
    values: tuple[float, ...] = ()
    source: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.device or not isinstance(self.device, str):
            raise ValueError(f"device 须为非空字符串: {self.device!r}")
        if not self.interface or not isinstance(self.interface, str):
            raise ValueError(f"interface 须为非空字符串: {self.interface!r}")
        if self.mode not in ("constant", "data_repeat", "data_predict"):
            raise ValueError(f"未知来源模式: {self.mode!r}")
        if len(self.values) != self.point_count:
            raise ValueError(
                f"序列长度须等于 point_count: {len(self.values)} x {self.point_count}"
            )
        object.__setattr__(self, "values", tuple(self.values))
        if not isinstance(self.source, Mapping):
            raise TypeError("source 须为 Mapping")
        object.__setattr__(
            self, "source", MappingProxyType(dict(self.source))
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "device": self.device,
            "interface": self.interface,
            "mode": self.mode,
            "resolution": self.resolution,
            "point_count": self.point_count,
            "values": list(self.values),
            "source": dict(self.source),
        }


@dataclass(frozen=True, slots=True)
class MaterializedSet:
    """物化返回边界（不可变值对象）。

    ``series`` 仅含非预测来源（按 device/interface 确定性排序）；
    ``pending_predict`` 为声明但不物化的预测绑定
    （{device, interface, target_type, object_id}，同序排列），是上游
    序列准备结果，不传入 Generator。series 为空且 pending 非空时不代表
    “已全部完成”。
    """

    series: tuple[MaterializedSeries, ...] = ()
    pending_predict: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))
        pending = tuple(self.pending_predict)
        for entry in pending:
            if not isinstance(entry, Mapping) or set(entry) != {
                "device",
                "interface",
                "target_type",
                "object_id",
            }:
                raise ValueError(f"pending_predict 条目形态非法: {entry!r}")
        object.__setattr__(
            self,
            "pending_predict",
            tuple(MappingProxyType(dict(e)) for e in pending),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "series": [s.to_dict() for s in self.series],
            "pending_predict": [dict(e) for e in self.pending_predict],
        }


def materialize_non_predictive_series(
    artifact: ValidatedAssemblyArtifact,
    resources: Mapping[str, bytes],
) -> MaterializedSet:
    """物化装配产物中全部非预测来源的全周期序列。

    ``resources`` 以 canonical 绑定 ``content.object_id``（字符串）为键
    显式提供只读字节（含预测绑定所需身份）；任一所需身份缺失即必要输入
    不完整，明确失败。不按 path 读取宿主机，不用最新数据替换，不改写
    artifact 与资源。
    """
    doc = json.loads(artifact.canonical_text)
    baseline_data = doc["project_baseline"]
    point_count = point_count_for_baseline(
        baseline_data["resolution"], baseline_data["leap_year"]
    )
    resolution = baseline_data["resolution"]
    series: list[MaterializedSeries] = []
    pending: list[dict[str, object]] = []
    devices = doc.get("devices") or {}
    for device_id in sorted(devices):
        bindings = devices[device_id].get("predefined_interfaces") or {}
        for interface_id in sorted(bindings):
            binding = bindings[interface_id]
            mode = binding["mode"]
            if mode == "constant":
                series.append(
                    MaterializedSeries(
                        device=device_id,
                        interface=interface_id,
                        mode=mode,
                        resolution=resolution,
                        point_count=point_count,
                        values=[binding["value"]] * point_count,
                        source={"kind": "constant"},
                    )
                )
            elif mode == "data_repeat":
                object_id = binding["content"]["object_id"]
                if object_id not in resources:
                    raise ValueError(
                        "固定资源缺失（必要输入不完整）: "
                        f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                    )
                cycle = _project_column(resources[object_id], interface_id)
                if not cycle:
                    raise ValueError(
                        "重复基线为空（违反已签发事实）: "
                        f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                    )
                period = len(cycle)
                series.append(
                    MaterializedSeries(
                        device=device_id,
                        interface=interface_id,
                        mode=mode,
                        resolution=resolution,
                        point_count=point_count,
                        values=tuple(cycle[i % period] for i in range(point_count)),
                        source={"kind": "data_repeat", "object_id": object_id},
                    )
                )
            elif mode == "data_predict":
                object_id = binding["content"]["object_id"]
                if object_id not in resources:
                    raise ValueError(
                        "固定资源缺失（必要输入不完整）: "
                        f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                    )
                pending.append(
                    {
                        "device": device_id,
                        "interface": interface_id,
                        "target_type": binding["target_type"],
                        "object_id": object_id,
                    }
                )
            else:
                raise ValueError(
                    f"未知来源模式（违反已签发事实）: {mode!r}"
                )
    return MaterializedSet(series=tuple(series), pending_predict=tuple(pending))


def _project_column(data: bytes, interface_id: str) -> list[float]:
    """从已确认 CSV 字节投影单列数值（文件顺序即 step 顺序）。

    只读取不校验：UTF-8 解码（BOM 按标准库语义），跳过前置 ``#`` 元数据
    行与空行（与校验器同一行语义），首个数据行为表头，按校验器同一
    归一化规则（去空白小写）定位契约列后逐行 float 转换。无宽容修复；
    内部损坏让实际 decode/csv/索引/float 错误可见。
    """
    lines = data.decode("utf-8-sig").splitlines()
    header_index = next(
        index
        for index, line in enumerate(lines)
        if line.strip() and not line.strip().startswith("#")
    )
    rows = list(csv.reader(lines[header_index:]))
    header = [cell.strip().lower() for cell in rows[0]]
    column = header.index(interface_id.lower())
    return [
        float(cells[column].strip())
        for cells in rows[1:]
        if any(cell.strip() for cell in cells)
    ]


__all__ = [
    "MaterializedSeries",
    "MaterializedSet",
    "materialize_non_predictive_series",
]
