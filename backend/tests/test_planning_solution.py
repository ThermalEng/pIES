"""规划解读取纯函数测试(S3-A)。

覆盖 writes._planning_params: 回执冻结名单筛出、同一快照正文绑定附着、
封存值/单位原样、同名首次为准、形态非法即数据损坏错误。DB 相关的 None
契约(无证据/无解/快照缺失/冻结名单或正文缺失)由 e2e 真实链覆盖, 此处
不伪造行。
"""

from __future__ import annotations

import pytest

from iesplan.application.results.writes import _planning_params
from iesplan.core.errors import AppError

NAMES = ["hp_cap", "bo_cap"]
BINDINGS = {
    "hp_cap": {"device_ref": "hp1", "parameter": "rated_heat_kw", "unit": "kW"},
    "bo_cap": {"device_ref": "bo1", "parameter": "rated_heat_kw", "unit": "kW"},
}


def test_filters_to_frozen_names() -> None:
    variables = [
        {"name": "hp_cap", "value": 320.5, "unit": "kW"},
        {"name": "hp1__electricity_in__t0", "value": 10.0, "unit": "kW"},
        {"name": "bo_cap", "value": 0.0, "unit": "kW"},
    ]
    params = _planning_params(variables, NAMES, BINDINGS)
    assert params == [
        {"variable": "hp_cap", "device_ref": "hp1", "parameter": "rated_heat_kw", "value": 320.5, "unit": "kW"},
        {"variable": "bo_cap", "device_ref": "bo1", "parameter": "rated_heat_kw", "value": 0.0, "unit": "kW"},
    ]


def test_duplicate_name_first_wins() -> None:
    variables = [
        {"name": "hp_cap", "value": 100.0, "unit": "kW"},
        {"name": "hp_cap", "value": 200.0, "unit": "kW"},
    ]
    params = _planning_params(variables, NAMES, BINDINGS)
    assert [p["value"] for p in params] == [100.0]


def test_illegal_value_or_unit_is_corrupt() -> None:
    with pytest.raises(AppError):
        _planning_params([{"name": "hp_cap", "value": "320", "unit": "kW"}], NAMES, BINDINGS)
    with pytest.raises(AppError):
        _planning_params([{"name": "hp_cap", "value": 320.0, "unit": None}], NAMES, BINDINGS)
    with pytest.raises(AppError):
        _planning_params([{"name": "hp_cap", "value": True, "unit": "kW"}], NAMES, BINDINGS)
    with pytest.raises(AppError):
        _planning_params([{"value": 1.0, "unit": "kW"}], NAMES, BINDINGS)


def test_missing_binding_is_corrupt() -> None:
    with pytest.raises(AppError):
        _planning_params(
            [{"name": "hp_cap", "value": 1.0, "unit": "kW"}],
            NAMES,
            {"hp_cap": {"device_ref": "hp1"}},
        )
    with pytest.raises(AppError):
        _planning_params(
            [{"name": "hp_cap", "value": 1.0, "unit": "kW"}],
            NAMES,
            {},
        )
