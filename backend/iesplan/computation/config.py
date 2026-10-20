"""CalculationConfig 公开契约入口(0.8 切片 C)。

只提供最小边界：配置 schema 文件加载与“schema 校验 → 不可变载体”
构造。能力核对（mode/精度/solver 兼容）在生成前由
``GeneratorRegistry.resolve`` 完成；多个 data_predict 时收敛是否必填
由后续 application 结合 artifact 判断，本模块不重读装配。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from iesplan.computation.protocols import CalculationConfig

SCHEMA_PATH = (
    pathlib.Path(__file__).parent / "schema" / "calculation-config-2.0.0.schema.json"
)


def load_calculation_config_schema() -> dict[str, Any]:
    """加载发布的计算配置 JSON Schema（包内资源，随包发布）。"""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_calculation_config(data: Mapping[str, Any]) -> CalculationConfig:
    """校验计算配置映射并构造不可变载体。

    依次执行：JSON Schema 校验（未知字段即拒绝）→ frozen
    ``CalculationConfig`` 构造（含领域规则：引用形态、精度枚举、
    目标去重、收敛正值、输出非空）。``to_dict`` 输出即规范字段顺序。
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"计算配置须为 Mapping，得到 {type(data).__name__}")
    schema = load_calculation_config_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(dict(data)))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(f"计算配置契约校验失败[{location or '<root>'}]: {first.message}")
    return CalculationConfig(**dict(data))


__all__ = [
    "SCHEMA_PATH",
    "load_calculation_config_schema",
    "validate_calculation_config",
]
