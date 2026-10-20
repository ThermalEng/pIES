"""全周期已物化序列公共契约。

最小确定性物化逻辑：``materialize_non_predictive_series`` 处理
constant/data_repeat/data_full，data_predict 待完成目标记入
``MaterializedSet.pending_predict``（保留固定资源身份）；预测训练与
完整预测序列由 ``materialize_predicted_series`` 经调用方给定的公开
predictor 完成，同一 ``MaterializedSeries`` 类型进入。

输入只消费 ``ValidatedAssemblyArtifact``（已签发事实）、调用方按
canonical 绑定 ``content.object_id``（字符串）显式传入的只读字节，
以及调用方（Worker 编排）从快照顶层评价确认块读取的已确认评价年限；
输出 step 恒为 ``0..point_count-1``，``point_count`` 由基线起始年、
分辨率与已确认评价年限推导的全周期总点数（复用 core 权威纯函数，
不重建基线对象、不重复校验）。

信任装配已确认的事实，不做任何重复校验：不调用数据解析校验器，
不复核 schema/单位/范围/step/周期，不产生诊断，不做 hash/摘要。
CSV 读取只做取数必需的确定性解码与列投影（标准库 csv 读取，不校验、
不修复、不 fallback）；固定资源缺失明确失败；其余违反已签发事实的
内部损坏让实际 decode/csv/索引/float 错误可见。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.computation.predictors import PredictedTarget, Predictor
from iesplan.core.contracts import horizon_point_count


@dataclass(frozen=True, slots=True)
class MaterializedSeries:
    """单个预定义接口的全周期计算序列（不可变值对象）。

    ``values`` 按 step 0 起连续排列，长度恒等于 ``point_count``；
    ``source`` 固定追溯（constant 无外部引用，data_repeat/data_full/
    data_predict 记 object_id）。``mode`` 为 constant、data_repeat、
    data_full、data_predict 四者之一；本函数只产出前三者，data_predict
    完成值以同一类型进入（训练与完整预测序列生成不属于本函数）。
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
        if self.mode not in ("constant", "data_repeat", "data_full", "data_predict"):
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
class MaterializedPriceSeries:
    """单个价格的全周期计算序列（不可变值对象，按 price_id 索引）。

    ``values`` 按 step 0 起连续排列，长度恒等于 ``point_count``（与
    求解域点数一致，供 Generator 逐点 ``price[t]×e[t]`` 计价）；
    ``source`` 固定追溯价格冻结对象（``{"kind": "price_series",
    "object_id": <oid>}``）。值填充在 F3b 由 Worker 物化完成，此处
    只定形态。
    """

    price_id: str
    point_count: int
    resolution: str
    unit: str
    values: tuple[float, ...] = ()
    source: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.price_id or not isinstance(self.price_id, str):
            raise ValueError(f"price_id 须为非空字符串: {self.price_id!r}")
        if len(self.values) != self.point_count:
            raise ValueError(
                f"价格序列长度须等于 point_count: {len(self.values)} x {self.point_count}"
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
            "price_id": self.price_id,
            "point_count": self.point_count,
            "resolution": self.resolution,
            "unit": self.unit,
            "values": list(self.values),
            "source": dict(self.source),
        }


@dataclass(frozen=True, slots=True)
class MaterializedSet:
    """物化返回边界（不可变值对象），即 Generator 第三项输入的集合形态。

    ``operation_years`` 为调用方从快照顶层评价确认块读取的已确认运营
    年限（必填，无默认值，不在此校验：提交边界唯一校验）；Generator
    以运行起始年/分辨率与该年限推导运营期总点数，``series`` 为空时仍有
    完整时域。``series`` 仅含非预测来源（按 device/interface 确定性排序）；
    ``pending_predict`` 为声明但不物化的预测绑定
    （{device, interface, target_type, object_id}，同序排列），是上游
    序列准备结果，不传入 Generator：传入 Generator 的集合须为完成态
    （``pending_predict`` 为空），由上游在预测产物合并后构成，本类型
    只做类型化区分，不在运行期复核。``prices`` 为已物化价格序列（按
    ``price_id`` 索引，供 Generator 对计费绑定逐点计价；值填充在 F3b
    由 Worker 物化完成）。
    """

    operation_years: int
    series: tuple[MaterializedSeries, ...] = ()
    pending_predict: tuple[Mapping[str, object], ...] = ()
    prices: tuple[MaterializedPriceSeries, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))
        object.__setattr__(self, "prices", tuple(self.prices))
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
            "operation_years": self.operation_years,
            "series": [s.to_dict() for s in self.series],
            "pending_predict": [dict(e) for e in self.pending_predict],
            "prices": [p.to_dict() for p in self.prices],
        }


def materialize_non_predictive_series(
    artifact: ValidatedAssemblyArtifact,
    resources: Mapping[str, bytes],
    *,
    operation_years: int,
) -> MaterializedSet:
    """物化装配产物中全部非预测来源的全周期序列。

    ``resources`` 以 canonical 绑定 ``content.object_id``（字符串）为键
    显式提供只读字节（含预测绑定所需身份）；任一所需身份缺失即必要输入
    不完整，明确失败。不按 path 读取宿主机，不用最新数据替换，不改写
    artifact 与资源。
    ``operation_years`` 为调用方从快照顶层评价确认块读取的已确认运营
    年限（必填，无默认值，不在此校验：提交边界唯一校验）；运营期总点数
    由运行起始年、分辨率与该年限推导。``data_full`` 原样采用运营期文件
    （运营期长度由提交闸门核对，此处只读不验；读取损坏按实际错误可见）。
    """
    doc = json.loads(artifact.canonical_text)
    baseline_data = doc["project_baseline"]
    resolution = baseline_data["resolution"]
    point_count = horizon_point_count(
        resolution, baseline_data["start_year"], operation_years
    )
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
            elif mode == "data_full":
                object_id = binding["content"]["object_id"]
                if object_id not in resources:
                    raise ValueError(
                        "固定资源缺失（必要输入不完整）: "
                        f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                    )
                full = _project_column(resources[object_id], interface_id)
                series.append(
                    MaterializedSeries(
                        device=device_id,
                        interface=interface_id,
                        mode=mode,
                        resolution=resolution,
                        point_count=point_count,
                        values=tuple(full),
                        source={"kind": "data_full", "object_id": object_id},
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
    return MaterializedSet(
        operation_years=operation_years,
        series=tuple(series),
        pending_predict=tuple(pending),
    )


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


def materialize_predicted_series(
    artifact: ValidatedAssemblyArtifact,
    resources: Mapping[str, bytes],
    predictions: tuple[Mapping[str, object], ...],
    resolve_predictor: Callable[[str], Predictor],
    seed: int | None,
    *,
    operation_years: int,
    previous: Mapping[str, object] | None = None,
    allow_subset: bool = False,
) -> tuple[PredictedTarget, ...]:
    """物化装配产物中 data_predict 绑定的全周期预测序列。

    ``predictions`` 为计算配置 ``predictions`` 原样条目
    (``{target, algorithm, parameters}``); ``resolve_predictor`` 为
    ``algorithm ref → Predictor`` 调用(目录由组合根持有, 此处只消费)。
    ``allow_subset`` 为 ``False`` 时 ``predictions`` 须覆盖全部绑定:
    配置↔绑定覆盖关系由提交闸门保证, 此处按 (实例, 接口) 直接配对,
    缺配对即实际结构错误可见失败, 不重跑提交校验。为 ``True`` 时只物化
    给定条目命中的绑定(耦合轮次只重算耦合目标, 独立产物由调用方复用)。
    历史列按绑定接口名投影; 目标类型不透明透传进产物与回执, 不在此解释。
    ``operation_years`` 为调用方从快照顶层评价确认块读取的已确认运营
    年限（必填，无默认值，不在此校验），预测序列按推导的运营期总点数
    生成。上一轮统一结果 ``previous`` 原样透传给各 predictor(自回归类算法
    使用；朴素算法忽略)。
    返回按 (实例, 接口) 稳定排序的预测产物。
    """
    doc = json.loads(artifact.canonical_text)
    baseline_data = doc["project_baseline"]
    point_count = horizon_point_count(
        baseline_data["resolution"], baseline_data["start_year"], operation_years
    )
    by_target = {}
    for entry in predictions:
        target = entry["target"]
        by_target[(str(target["instance"]), str(target["interface"]))] = entry
    devices = doc.get("devices") or {}
    predicted: list[PredictedTarget] = []
    for device_id in sorted(devices):
        bindings = devices[device_id].get("predefined_interfaces") or {}
        for interface_id in sorted(bindings):
            binding = bindings[interface_id]
            if not isinstance(binding, Mapping) or binding.get("mode") != "data_predict":
                continue
            key = (str(device_id), str(interface_id))
            if key not in by_target:
                if allow_subset:
                    continue
                raise KeyError(key)  # 提交闸门已保证覆盖, 缺配对即快照故障
            entry = by_target[key]
            predictor: Predictor = resolve_predictor(str(entry["algorithm"]))
            # 算法↔目标类型相容已由提交闸门 TASK-REQ-014 校核, 此处不重复验证。
            target_type = str(binding["target_type"])
            object_id = str(binding["content"]["object_id"])
            if object_id not in resources:
                raise ValueError(
                    "固定资源缺失（必要输入不完整）: "
                    f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                )
            history = tuple(_project_column(resources[object_id], str(interface_id)))
            # 参数形态已由配置冻结入口校核, 此处直接使用, 不重复验证。
            parameters = entry.get("parameters") or {}
            predicted.append(
                predictor.predict(
                    device=str(device_id),
                    interface=str(interface_id),
                    target_type=target_type,
                    history=history,
                    history_object_id=object_id,
                    parameters=parameters,
                    seed=seed,
                    point_count=point_count,
                    previous=previous,
                )
            )
    return tuple(predicted)


__all__ = [
    "MaterializedPriceSeries",
    "MaterializedSeries",
    "MaterializedSet",
    "materialize_non_predictive_series",
    "materialize_predicted_series",
]
