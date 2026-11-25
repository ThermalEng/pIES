"""Target 1.2.0 CalculationConfig contract (13 fields, slotted options, parallel evaluators)."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NoReturn

from iesplan.computation.contracts.common import check_finite_json, validate_against_schema
from iesplan.computation.protocols import check_provider_ref, freeze_json_value, thaw_json_value

TARGET_CALCULATION_CONFIG_SCHEMA = "ies.calculation-config"
TARGET_CALCULATION_CONFIG_VERSION = "1.0.0"

#: Authoritative 13-field order for 1.2.0 CalculationConfig.
TARGET_CALCULATION_CONFIG_FIELDS = (
    "schema",
    "schema_version",
    "mode",
    "predictions",
    "generator",
    "solver",
    "algorithm",
    "evaluators",
    "precision",
    "time_limit_seconds",
    "options",
    "seed",
    "outputs",
)

TARGET_CALCULATION_CONFIG_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "schema"
    / "calculation-config-target-1.0.0.schema.json"
)


def load_target_calculation_config_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for 1.2.0 CalculationConfig."""
    return json.loads(TARGET_CALCULATION_CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))


def _freeze_prediction_entry(entry: object) -> Mapping[str, object]:
    if not isinstance(entry, Mapping) or set(entry) != {"target", "algorithm", "parameters"}:
        raise ValueError(f"prediction 条目须恰为 target/algorithm/parameters 三键: {entry!r}")
    target = entry["target"]
    if not isinstance(target, Mapping) or set(target) != {"instance", "interface"}:
        raise ValueError(f"prediction.target 须恰为 instance/interface 二键: {target!r}")
    for key in ("instance", "interface"):
        if not isinstance(target[key], str) or not target[key]:
            raise ValueError(f"prediction.target.{key} 须为非空字符串: {target.get(key)!r}")
    check_provider_ref(entry["algorithm"], "prediction.algorithm")
    parameters = entry["parameters"]
    if not isinstance(parameters, Mapping):
        raise TypeError("prediction.parameters 须为 Mapping")
    check_finite_json(parameters, "prediction.parameters")
    return freeze_json_value(
        {
            "target": {"instance": target["instance"], "interface": target["interface"]},
            "algorithm": entry["algorithm"],
            "parameters": dict(parameters),
        }
    )


def _freeze_predictions(predictions: object) -> tuple[Mapping[str, object], ...]:
    if predictions is None:
        raise TypeError("predictions 不得为 None，使用空列表表示无预测")
    if isinstance(predictions, Mapping) or not isinstance(predictions, (list, tuple)):
        raise TypeError(f"predictions 须为序列，得到 {type(predictions).__name__}")
    frozen: list[Mapping[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for entry in predictions:
        item = _freeze_prediction_entry(entry)
        target = item["target"]
        assert isinstance(target, Mapping)
        key = (str(target["instance"]), str(target["interface"]))
        if key in seen:
            raise ValueError(f"prediction.target 不得重复: {key!r}")
        seen.add(key)
        frozen.append(item)
    return tuple(frozen)


def _freeze_evaluators(evaluators: object) -> tuple[Mapping[str, object], ...]:
    if evaluators is None:
        raise TypeError("evaluators 不得为 None，使用空列表表示无评价器")
    if isinstance(evaluators, Mapping) or not isinstance(evaluators, (list, tuple)):
        raise TypeError(f"evaluators 须为序列，得到 {type(evaluators).__name__}")
    frozen: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for entry in evaluators:
        if not isinstance(entry, Mapping) or set(entry) != {"evaluator", "parameters"}:
            raise ValueError(f"evaluators 条目须恰为 evaluator/parameters 二键: {entry!r}")
        evaluator_ref = check_provider_ref(entry["evaluator"], "evaluators.evaluator")
        if evaluator_ref in seen:
            raise ValueError(f"evaluators.evaluator 不得重复: {evaluator_ref!r}")
        seen.add(evaluator_ref)
        parameters = entry["parameters"]
        if not isinstance(parameters, Mapping):
            raise TypeError("evaluators.parameters 须为 Mapping")
        check_finite_json(parameters, "evaluators.parameters")
        frozen.append(
            freeze_json_value(
                {
                    "evaluator": evaluator_ref,
                    "parameters": dict(parameters),
                }
            )
        )
    return tuple(frozen)


def _freeze_slotted_options(options: object, *, is_unified: bool) -> Mapping[str, object]:
    if not isinstance(options, Mapping):
        raise TypeError(f"options 须为 Mapping（可用空对象，不得为 None），得到 {type(options).__name__}")
    allowed_slots = {"algorithm"} if is_unified else {"generator", "solver"}
    extra = sorted(k for k in options if not isinstance(k, str) or k not in allowed_slots)
    if extra:
        mode_name = "一体化" if is_unified else "分步"
        raise ValueError(f"{mode_name}模式下 options 仅允许 {sorted(allowed_slots)} 槽位，非法键: {extra!r}")
    normalized: dict[str, object] = {}
    for slot, slot_val in options.items():
        if not isinstance(slot_val, Mapping):
            raise TypeError(f"options.{slot} 须为 Mapping，得到 {type(slot_val).__name__}")
        check_finite_json(slot_val, f"options.{slot}")
        normalized[slot] = dict(slot_val)
    return freeze_json_value(normalized)


@dataclass(frozen=True, slots=True)
class TargetCalculationConfig:
    """1.2.0 13-field immutable CalculationConfig value object."""

    schema: str = TARGET_CALCULATION_CONFIG_SCHEMA
    schema_version: str = TARGET_CALCULATION_CONFIG_VERSION
    mode: str = ""
    predictions: tuple[Mapping[str, object], ...] = ()
    generator: str | None = None
    solver: str | None = None
    algorithm: str | None = None
    evaluators: tuple[Mapping[str, object], ...] = ()
    precision: str = ""
    time_limit_seconds: int = 0
    options: Mapping[str, object] = field(default_factory=dict)
    seed: int | None = None
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != TARGET_CALCULATION_CONFIG_SCHEMA:
            raise ValueError(f"计算配置 schema 非法: {self.schema!r}")
        if self.schema_version != TARGET_CALCULATION_CONFIG_VERSION:
            raise ValueError(f"计算配置版本非法: {self.schema_version!r}")
        if not isinstance(self.mode, str) or not self.mode:
            raise ValueError(f"mode 须为非空字符串: {self.mode!r}")
        if self.generator is not None:
            check_provider_ref(self.generator, "generator")
        if self.solver is not None:
            check_provider_ref(self.solver, "solver")
        if self.algorithm is not None:
            check_provider_ref(self.algorithm, "algorithm")
        is_unified = self.algorithm is not None
        if is_unified:
            if self.generator is not None or self.solver is not None:
                raise ValueError("一体化算法模式(algorithm 非空)下 generator 与 solver 必须为 None")
        elif self.generator is None or self.solver is None:
            raise ValueError("分步算法模式下 generator 与 solver 必须同时提供精确引用（不能为 None）")
        if self.precision not in ("low", "medium", "high"):
            raise ValueError(f"precision 须为 low|medium|high: {self.precision!r}")
        if (
            not isinstance(self.time_limit_seconds, int)
            or isinstance(self.time_limit_seconds, bool)
            or self.time_limit_seconds <= 0
        ):
            raise ValueError(f"time_limit_seconds 须为正整数: {self.time_limit_seconds!r}")
        if self.seed is not None and (not isinstance(self.seed, int) or isinstance(self.seed, bool)):
            raise TypeError("seed 须为整数或 None（不接受布尔值）")
        if isinstance(self.outputs, Mapping) or not isinstance(self.outputs, (list, tuple)):
            raise TypeError(f"outputs 须为字符串序列，得到 {type(self.outputs).__name__}")
        outputs = tuple(self.outputs)
        if not outputs:
            raise ValueError("outputs 不得为空")
        for name in outputs:
            if not isinstance(name, str) or not name:
                raise ValueError(f"输出请求须为非空字符串: {name!r}")
        object.__setattr__(self, "predictions", _freeze_predictions(self.predictions))
        object.__setattr__(self, "evaluators", _freeze_evaluators(self.evaluators))
        object.__setattr__(self, "options", _freeze_slotted_options(self.options, is_unified=is_unified))
        object.__setattr__(self, "outputs", outputs)

    @property
    def is_unified(self) -> bool:
        """Return True if this configuration selects a unified algorithm plugin."""
        return self.algorithm is not None

    def slot_options(self, slot: str) -> Mapping[str, object]:
        """Return frozen parameters mapping for a stage slot (defaults to empty mapping)."""
        value = self.options.get(slot)
        if value is None:
            return MappingProxyType({})
        assert isinstance(value, Mapping)
        return value

    def to_dict(self) -> dict[str, Any]:
        """Serialize to an independent JSON-compatible dictionary in 13-field order."""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "predictions": [dict(thaw_json_value(entry)) for entry in self.predictions],
            "generator": self.generator,
            "solver": self.solver,
            "algorithm": self.algorithm,
            "evaluators": [dict(thaw_json_value(entry)) for entry in self.evaluators],
            "precision": self.precision,
            "time_limit_seconds": self.time_limit_seconds,
            "options": thaw_json_value(self.options),
            "seed": self.seed,
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_frozen(cls, frozen: Mapping[str, Any]) -> TargetCalculationConfig:
        """Restore an immutable carrier from already-validated snapshot data without re-validating."""
        instance = object.__new__(cls)
        object.__setattr__(instance, "schema", frozen["schema"])
        object.__setattr__(instance, "schema_version", frozen["schema_version"])
        object.__setattr__(instance, "mode", frozen["mode"])
        object.__setattr__(
            instance,
            "predictions",
            tuple(freeze_json_value(entry) for entry in frozen["predictions"]),
        )
        object.__setattr__(instance, "generator", frozen["generator"])
        object.__setattr__(instance, "solver", frozen["solver"])
        object.__setattr__(instance, "algorithm", frozen["algorithm"])
        object.__setattr__(
            instance,
            "evaluators",
            tuple(freeze_json_value(entry) for entry in frozen["evaluators"]),
        )
        object.__setattr__(instance, "precision", frozen["precision"])
        object.__setattr__(instance, "time_limit_seconds", frozen["time_limit_seconds"])
        object.__setattr__(instance, "options", freeze_json_value(frozen["options"]))
        object.__setattr__(instance, "seed", frozen["seed"])
        object.__setattr__(instance, "outputs", tuple(frozen["outputs"]))
        return instance


def validate_target_calculation_config(data: Mapping[str, Any]) -> TargetCalculationConfig:
    """Validate a raw 13-field CalculationConfig mapping against schema and domain rules."""
    if not isinstance(data, Mapping):
        raise TypeError(f"计算配置须为 Mapping，得到 {type(data).__name__}")
    raw = dict(data)
    check_finite_json(raw, "calculation_config")
    validate_against_schema(
        load_target_calculation_config_schema(),
        raw,
        error_prefix="计算配置契约校验失败",
    )
    return TargetCalculationConfig(**raw)


def _reject_json_constant(token: str) -> NoReturn:
    raise ValueError(f"计算配置禁止非有限 JSON 常量: {token!r}")


def parse_target_calculation_config_json(raw: str | bytes) -> TargetCalculationConfig:
    """Parse JSON text/bytes rejecting NaN/Infinity tokens, then validate CalculationConfig."""
    parsed = json.loads(raw, parse_constant=_reject_json_constant)
    return validate_target_calculation_config(parsed)
