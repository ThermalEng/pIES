"""计算类型化输入契约(快照已签发事实的运行期表示映射)。

快照创建/持久化适配边界已签发 ``ValidatedAssemblyArtifact``(规范文本 +
校验回执二件套): 运行期把 DB 封存 JSON 装配进 frozen 类型只做表示映射,
不重跑装配校验、不复判阻断、不复核哈希、不补默认值, 亦不设
``trusted``/``skip_validation`` 后门——直接消费已签发类型本身。

- 封存缺字段或形状非法 → ``ValueError`` 表示映射失败(调用方错误);
- ``ComputeResources`` 只含快照真实固定的内容(数据集版本清单与容差);
  对象字节等 0.8 内容链未接通(计算侧无存储读取, 完整资源内容未来由
  调用方随 provider 显式传入);
- 全部容器构造时冻结, 不与快照记录跨模块共享可变 ``dict``。

本模块不实现任何 0.8 求解器/生成器算法。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.core.diagnostics import Diagnostic
from iesplan.computation.protocols import CalculationConfig
from iesplan.tasks.contracts import CalcSnapshotRecord

__all__ = [
    "ComputeInputs",
    "ComputeResources",
    "build_compute_inputs",
]

#: 快照计算配置中求解选项的封存键(缺失即表示映射失败, 不猜默认值)。
_CONFIG_PARAMS_KEY = "params"
#: 快照独立容差字段并入求解选项的键(快照未固定时不出现该键)。
_TOLERANCES_OPTION_KEY = "tolerances"


def _freeze_value(value: object) -> object:
    """递归冻结封存值(dict → MappingProxy, list/tuple → tuple, 其余原样)。"""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: object) -> object:
    """递归解冻为普通 dict/list(与 ``_freeze_value`` 互逆)。"""
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ComputeResources(Mapping):
    """计算已固定资源(快照真实固定内容的不可变契约, 亦为只读 Mapping)。

    映射视图固定为 ``{"dataset_version_ids", "tolerances"}`` 两键;
    0.8 内容链(对象字节)未接通, 不在此虚构。
    """

    dataset_version_ids: tuple[int, ...] = ()
    tolerances: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dataset_version_ids", tuple(self.dataset_version_ids)
        )
        object.__setattr__(self, "tolerances", _freeze_value(self.tolerances))

    def __getitem__(self, key: str) -> object:
        return self._as_mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._as_mapping())

    def __len__(self) -> int:
        return len(self._as_mapping())

    def _as_mapping(self) -> dict[str, object]:
        return {
            "dataset_version_ids": self.dataset_version_ids,
            "tolerances": self.tolerances,
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "dataset_version_ids": list(self.dataset_version_ids),
            "tolerances": _thaw_value(self.tolerances),
        }


@dataclass(frozen=True, slots=True)
class ComputeInputs:
    """一次计算的已准备输入(快照已固定值, 不可变的类型化契约)。"""

    artifact: ValidatedAssemblyArtifact
    resources: ComputeResources
    config: CalculationConfig


def _map_diagnostic(raw: object) -> Diagnostic:
    """封存诊断稳定字典 → ``Diagnostic``(字段结构直装, 不复判阻断)。"""
    if not isinstance(raw, Mapping):
        raise ValueError("装配回执诊断条目须为 Mapping")
    try:
        location = raw["location"]
        if location is not None and not isinstance(location, Mapping):
            raise ValueError("装配回执诊断 location 须为 Mapping 或 None")
        params = raw["params"]
        if not isinstance(params, Mapping):
            raise ValueError("装配回执诊断 params 须为 Mapping")
        return Diagnostic(
            code=raw["code"],
            severity=raw["severity"],
            blocking=raw["blocking"],
            message_key=raw["message_key"],
            params=dict(params),
            location=None if location is None else dict(location),
            fix_hint_key=raw["fix_hint_key"],
            ref_ids=tuple(raw["ref_ids"]),
            suppressed=raw["suppressed"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"装配回执诊断表示映射失败: {exc}") from exc


def _map_receipt(stored: Mapping[str, Any]) -> ValidationReceipt:
    """封存回执字典 → ``ValidationReceipt``(签发字头原样携带, 不校验)。"""
    try:
        diagnostics = stored["diagnostics"]
        if not isinstance(diagnostics, (list, tuple)):
            raise ValueError("装配回执 diagnostics 须为列表")
        validator = stored["validator"]
        canonical_algorithm = stored["canonical_algorithm"]
        dependencies = stored["dependencies"]
        resources = stored["resources"]
        if not isinstance(validator, Mapping) or not isinstance(
            canonical_algorithm, Mapping
        ):
            raise ValueError("装配回执 validator/canonical_algorithm 须为 Mapping")
        if not isinstance(dependencies, Mapping) or not isinstance(resources, Mapping):
            raise ValueError("装配回执 dependencies/resources 须为 Mapping")
        return ValidationReceipt(
            schema_id=stored["schema"],
            schema_version=stored["schema_version"],
            validator_id=validator["id"],
            validator_version=validator["version"],
            canonical_algorithm_id=canonical_algorithm["id"],
            canonical_algorithm_version=canonical_algorithm["version"],
            dependencies=dict(dependencies),
            resources=dict(resources),
            diagnostics=tuple(_map_diagnostic(raw) for raw in diagnostics),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"快照装配回执表示映射失败(封存缺字段或形状非法): {exc}") from exc


def _map_artifact(canonical_text: str, stored: Mapping[str, Any]) -> ValidatedAssemblyArtifact:
    """封存二件套 → 已签发装配产物(表示映射, 不重跑签发校验)。"""
    return ValidatedAssemblyArtifact(
        canonical_text=canonical_text, receipt=_map_receipt(stored)
    )


def _map_config(
    calc_config_snapshot: object,
    *,
    seed: int | None,
    tolerances: Mapping[str, Any] | None,
) -> CalculationConfig:
    """快照已固定值 → ``CalculationConfig``(params 缺失即映射失败, 不补默认)。"""
    try:
        if not isinstance(calc_config_snapshot, Mapping):
            raise ValueError("快照计算配置须为 Mapping")
        params = calc_config_snapshot[_CONFIG_PARAMS_KEY]
        if not isinstance(params, Mapping):
            raise ValueError("快照计算配置 params 须为 Mapping")
        options: dict[str, object] = dict(params)
        if tolerances is not None:
            if not isinstance(tolerances, Mapping):
                raise ValueError("快照 tolerances 须为 Mapping 或 None")
            options[_TOLERANCES_OPTION_KEY] = dict(tolerances)
        return CalculationConfig(seed=seed, options=options)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"快照计算配置表示映射失败(封存缺字段或形状非法): {exc}"
        ) from exc


def build_compute_inputs(snapshot: CalcSnapshotRecord | None) -> ComputeInputs:
    """由快照记录准备计算输入(纯函数, 无会话、无事务、无复校验)。

    - 快照缺失/规范装配文本缺失/回执缺失或形状非法 → ``ValueError``(未签发的
      装配输入不得生成 Bundle, 不产生部分产物);
    - 封存表示缺字段或形状非法 → ``ValueError`` 表示映射失败, 不补默认值。
    """
    if snapshot is None:
        raise ValueError("计算阶段缺快照记录, 无法准备计算输入")
    canonical_text = snapshot.canonical_assembly_text
    if not canonical_text or not canonical_text.strip():
        raise ValueError("快照缺规范装配文本(未签发的装配输入不得生成 Bundle)")
    receipt_dict = snapshot.assembly_receipt
    if not isinstance(receipt_dict, Mapping):
        raise ValueError("快照缺装配回执(未签发的装配输入不得生成 Bundle)")
    artifact = _map_artifact(canonical_text, receipt_dict)
    resources = ComputeResources(
        dataset_version_ids=tuple(snapshot.dataset_version_ids),
        tolerances=dict(snapshot.tolerances or {}),
    )
    config = _map_config(
        snapshot.calc_config_snapshot,
        seed=snapshot.random_seed,
        tolerances=snapshot.tolerances,
    )
    return ComputeInputs(artifact=artifact, resources=resources, config=config)
