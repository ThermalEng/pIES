"""公开预测能力(0.12 B5-2): 预测算法 descriptor、独立原子注册表与首个确定性 predictor。

- 预测算法以 ``CalculationConfig.predictions[].algorithm`` 精确引用
  (``<稳定ID>@<语义化版本>``)选择, 与 generator/solver/executor/result
  adapter 同一引用语义, 不回退、不猜测;
- ``PredictorDescriptor`` 只声明结构性需求: 是否依赖上一轮求解结果
  (必填 ``depends_on_solve_result``)、是否接受任意目标类型
  (否则列出显式支持名)、是否需要历史序列。目标类型取值本身不透明
  透传, 不在此建立类型注册表(多种目标类型均可工作, 以定向测试锁定);
- predictor 为纯函数: 历史序列 + 参数 + 种子 + 全周期点数 → 完整预测
  序列值 + 不可变训练产物字节 + 回执基底(JSON, 训练产物引用由提交
  密封后回填)。不在 Worker/Generator 内训练; 本模块不接触数据库、
  存储与网络;
- 每个 provider 以 ``validate_parameters`` 在计算配置输入边界校验自己的
  参数; 耦合 provider 另以 ``measure_convergence`` 对相邻两轮预测产物
  与只读计算输出计算无量纲收敛量(归一化与尺度由算法版本化参数规则
  定义, 平台不统一规定);
- 注册表由 bootstrap 组合根持有并发布; 未注册引用统一抛
  ``ComputationUnavailableError``, 不回退。耦合能力在注册时确认收敛
  调用可用, 不到每轮重复探测。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from iesplan.computation.errors import ComputationUnavailableError

#: 首个内置 predictor 精确引用(与 calculation-config 文档示例同一)。
NAIVE_PREDICTOR_REF = "ies.predictor.naive@1.0.0"

#: 训练产物字节媒体类型(确定性 JSON)。
TRAINING_ARTIFACT_MEDIA_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class PredictorDescriptor:
    """单个预测算法的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``; 配置 ``algorithm`` 与本 ``ref``
    相等才表示选中。``depends_on_solve_result`` 为必填布尔: ``False`` 为
    独立预测(求解前生成一次), ``True`` 为耦合预测(消费上一轮求解结果);
    不设默认值, 省略即构造失败, 防止未声明被误认为独立。
    ``accepts_any_target_type`` 为真时接受任意装配声明
    的目标类型(不透明透传); 为假时仅接受 ``target_types`` 所列。
    ``needs_history`` 声明是否需要历史序列输入。
    """

    ref: str
    depends_on_solve_result: bool
    accepts_any_target_type: bool = True
    target_types: tuple[str, ...] = ()
    needs_history: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.depends_on_solve_result, bool):
            raise TypeError("depends_on_solve_result 须为显式布尔值")

    def supports(self, target_type: str) -> bool:
        """该目标类型是否可用本算法预测。"""
        return bool(self.accepts_any_target_type) or target_type in self.target_types


@dataclass(frozen=True, slots=True)
class PredictedTarget:
    """单次单目标预测产物(不可变值对象)。

    ``values`` 长度恒等于请求点数; ``artifact`` 为不可变训练产物字节;
    ``receipt`` 为回执基底(JSON 兼容, 不含训练产物引用, 由提交密封后回填)。
    """

    device: str
    interface: str
    target_type: str
    values: tuple[float, ...] = ()
    history_object_id: str = ""
    artifact: bytes = b""
    artifact_media_type: str = TRAINING_ARTIFACT_MEDIA_TYPE
    receipt: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PredictionConvergence:
    """单目标单轮无量纲收敛量(不可变值对象, 耦合算法的公开返回值)。

    ``metric`` 为算法版本化规则定义的指标标识(非空字符串);
    ``value`` 为非负有限数(无量纲, 归一化已由算法完成, 不混比不同
    物理单位); ``unit`` 固定为 ``"1"``。
    """

    metric: str
    value: float
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric:
            raise TypeError("convergence metric 须为非空字符串")
        value = self.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("convergence value 须为数值(不接受布尔值)")
        if not math.isfinite(value) or value < 0:
            raise ValueError("convergence value 须为非负有限数")
        object.__setattr__(self, "value", float(value))
        if self.unit != "1":
            raise ValueError("convergence unit 固定为 '1'(无量纲)")


@runtime_checkable
class Predictor(Protocol):
    """预测算法公开身份(结构协议)。"""

    @property
    def descriptor(self) -> PredictorDescriptor:
        """本算法公开描述。"""
        ...

    def validate_parameters(self, parameters: Mapping[str, object]) -> None:
        """校验本算法公开参数(计算配置输入边界唯一调用)。

        形态(是否为 Mapping、是否有限 JSON)已由配置载体保证;
        此处只检查算法自身的版本化参数规则, 不满足即 ``ValueError``
        指明字段。执行阶段直接使用已固定参数, 不重复调用。
        """
        ...

    def predict(
        self,
        *,
        device: str,
        interface: str,
        target_type: str,
        history: tuple[float, ...],
        history_object_id: str,
        parameters: Mapping[str, object],
        seed: int | None,
        point_count: int,
        previous: Mapping[str, object] | None = None,
    ) -> PredictedTarget:
        """由历史序列生成全周期预测值。"""
        ...


@runtime_checkable
class CoupledPredictor(Predictor, Protocol):
    """耦合预测算法公开身份: 独立能力之外, 另提供纯收敛度量。

    首轮 ``previous_outputs`` 为 ``None`` 时不调用本方法(首轮没有相邻轮
    比较); 后续每轮求解完成后调用一次。
    """

    def measure_convergence(
        self,
        *,
        previous_prediction: PredictedTarget,
        current_prediction: PredictedTarget,
        previous_outputs: Mapping[str, object] | None,
        current_outputs: Mapping[str, object],
        parameters: Mapping[str, object],
    ) -> PredictionConvergence:
        """由相邻两轮预测产物与只读计算输出计算本目标无量纲收敛量。

        纯函数: 只读入参, 不接触数据库、存储与网络。所需尺度来自
        ``parameters`` 中本算法版本化规则定义的参数(平台不统一规定)。
        """
        ...


class PredictorRegistry:
    """已发布的预测算法目录(不可变快照语义, 实例级持有, 无模块全局状态)。"""

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, Predictor]) -> None:
        self._providers = dict(providers)

    def __len__(self) -> int:
        return len(self._providers)

    def snapshot(self) -> tuple[PredictorDescriptor, ...]:
        """确定性快照(按引用排序, 只公开 descriptor 数据)。"""
        return tuple(self._providers[key].descriptor for key in sorted(self._providers))

    def get(self, ref: str) -> Predictor:
        """按精确引用取真实 predictor; 未知引用即明确不可用。"""
        try:
            return self._providers[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"预测算法不可用（未注册）: {ref!r}", reason="no-predictor"
            ) from None


def build_predictor_registry(providers: tuple[Predictor, ...]) -> PredictorRegistry:
    """原子发布预测算法目录: 全部校验通过后一次性发布。

    任一候选协议不符、重复 ref、缺少参数校验、耦合缺少收敛度量即整体
    拒绝, 不发布部分状态。收敛调用可用性在此确定, 不到每轮重复探测。
    """
    seen: dict[str, Predictor] = {}
    for provider in providers:
        try:
            descriptor = provider.descriptor
        except AttributeError as exc:
            raise ComputationUnavailableError(
                "预测算法 descriptor 缺失", reason="bad-predictor-descriptor"
            ) from exc
        if not isinstance(descriptor, PredictorDescriptor) or not descriptor.ref:
            raise ComputationUnavailableError(
                "预测算法 descriptor 非法", reason="bad-predictor-descriptor"
            )
        if not callable(getattr(provider, "validate_parameters", None)):
            raise ComputationUnavailableError(
                f"预测算法缺少参数校验: {descriptor.ref!r}",
                reason="bad-predictor-parameters",
            )
        if descriptor.depends_on_solve_result and not callable(
            getattr(provider, "measure_convergence", None)
        ):
            raise ComputationUnavailableError(
                f"耦合预测算法缺少收敛度量: {descriptor.ref!r}",
                reason="bad-predictor-convergence",
            )
        if descriptor.ref in seen:
            raise ComputationUnavailableError(
                f"预测算法重复注册: {descriptor.ref!r}", reason="duplicate-predictor"
            )
        seen[descriptor.ref] = provider
    return PredictorRegistry(seen)


def check_convergence(value: object) -> PredictionConvergence:
    """校验耦合算法返回的收敛量(自定义能力公开输出边界)。

    非 ``PredictionConvergence`` 即明确不可用; 字段合法性由其构造时
    保证, 此处不重复核对字段。
    """
    if not isinstance(value, PredictionConvergence):
        raise ComputationUnavailableError(
            f"预测收敛量返回形态非法: {type(value).__name__!r}",
            reason="bad-predictor-convergence",
        )
    return value


def split_predictions_by_dependency(
    predictions: tuple[Mapping[str, object], ...],
    resolve_predictor: Callable[[str], Predictor],
) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    """按依赖声明拆分配置预测条目 → (独立条目, 耦合条目), 各组保持配置稳定序。

    只读各算法已注册的 ``depends_on_solve_result`` 声明; 未知算法引用
    即明确不可用。禁止按目标数量、目标类型或猜测 previous 使用情况分流。
    """
    independent: list[Mapping[str, object]] = []
    coupled: list[Mapping[str, object]] = []
    for entry in predictions:
        predictor = resolve_predictor(str(entry["algorithm"]))
        if predictor.descriptor.depends_on_solve_result:
            coupled.append(entry)
        else:
            independent.append(entry)
    return tuple(independent), tuple(coupled)


def _artifact_bytes(
    *,
    algorithm: str,
    parameters: Mapping[str, object],
    seed: int | None,
    device: str,
    interface: str,
    target_type: str,
    history_object_id: str,
    history_points: int,
    learned: Mapping[str, object],
) -> bytes:
    """训练产物确定性字节(稳定键序 JSON; 相同输入恒得相同字节)。"""
    return json.dumps(
        {
            "schema": "ies.prediction-artifact",
            "algorithm": algorithm,
            "parameters": dict(parameters),
            "seed": seed,
            "target": {"device": device, "interface": interface, "target_type": target_type},
            "history": {"object_id": history_object_id, "points": history_points},
            "learned": dict(learned),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class NaivePredictor:
    """朴素携载 predictor(首个真实预测能力, 全确定性)。

    语义: 以历史序列最后一个有限值携载全周期(末值保持)。种子不参与
    数值计算, 只记入产物与回执以保可复现追溯。忽略 ``previous``(上一轮
    结果) —— 自回归类算法由后续 predictor 实现, 本算法不伪装使用。
    接受任意目标类型(结构与目标类型无关); 空历史即结构错误, 不补零。
    """

    @property
    def descriptor(self) -> PredictorDescriptor:
        return PredictorDescriptor(
            ref=NAIVE_PREDICTOR_REF,
            depends_on_solve_result=False,
            accepts_any_target_type=True,
            needs_history=True,
            description="末值携载(全确定性, 种子只追溯不参与计算)",
        )

    def validate_parameters(self, parameters: Mapping[str, object]) -> None:
        """朴素算法无参数约束: 接受任意映射(形态已由配置载体保证)。"""
        return None

    def predict(
        self,
        *,
        device: str,
        interface: str,
        target_type: str,
        history: tuple[float, ...],
        history_object_id: str,
        parameters: Mapping[str, object],
        seed: int | None,
        point_count: int,
        previous: Mapping[str, object] | None = None,
    ) -> PredictedTarget:
        if not history:
            raise ValueError(
                f"朴素预测无历史可携: {device!r} x {interface!r}"
            )
        last = history[-1]
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            raise ValueError(
                f"朴素预测历史末值非数值: {device!r} x {interface!r} x {last!r}"
            )
        values = tuple(float(last) for _ in range(point_count))
        artifact = _artifact_bytes(
            algorithm=NAIVE_PREDICTOR_REF,
            parameters=parameters,
            seed=seed,
            device=device,
            interface=interface,
            target_type=target_type,
            history_object_id=history_object_id,
            history_points=len(history),
            learned={"method": "naive_carry", "last_value": float(last)},
        )
        receipt: dict[str, object] = {
            "target_type": target_type,
            "algorithm": NAIVE_PREDICTOR_REF,
            "parameters": dict(parameters),
            "feature_order": ["history_value"],
            "target_order": [interface],
            "training_input_refs": {"history": history_object_id},
            # 朴素携载的学习目标即历史对象接口列末值：真实对象引用，
            # 非推断；未来协变量无装配绑定字段，契约待定，保持空。
            "training_target_refs": {interface: history_object_id},
            "covariate_refs": {},
            "seed": seed,
            "output_series": {
                "device": device,
                "interface": interface,
                "mode": "data_predict",
                "point_count": point_count,
            },
        }
        return PredictedTarget(
            device=device,
            interface=interface,
            target_type=target_type,
            values=values,
            history_object_id=history_object_id,
            artifact=artifact,
            receipt=receipt,
        )


#: 内置 predictor(组合根显式注册, 无动态发现)。
NAIVE_PREDICTOR = NaivePredictor()


def prediction_delta(previous: Sequence[float], current: Sequence[float]) -> float:
    """两轮预测序列最大绝对差(阶段二收敛量)。

    键序/点数不一致即结构错误, 不截断对齐。
    """
    prev = tuple(previous)
    curr = tuple(current)
    if len(prev) != len(curr):
        raise ValueError(
            f"预测序列轮次点数不一致: {len(prev)} x {len(curr)}"
        )
    if not curr:
        raise ValueError("预测序列为空, 无法比较收敛")
    return max(abs(new - old) for old, new in zip(prev, curr, strict=True))


__all__ = [
    "NAIVE_PREDICTOR",
    "NAIVE_PREDICTOR_REF",
    "TRAINING_ARTIFACT_MEDIA_TYPE",
    "CoupledPredictor",
    "PredictedTarget",
    "PredictionConvergence",
    "Predictor",
    "PredictorDescriptor",
    "PredictorRegistry",
    "NaivePredictor",
    "build_predictor_registry",
    "check_convergence",
    "prediction_delta",
    "split_predictions_by_dependency",
]
