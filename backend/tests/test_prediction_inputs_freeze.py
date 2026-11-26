"""预测输入冻结核对(提交闸门): 配置 predictions 与装配 data_predict 绑定双向匹配 + 算法相容。

纯函数矩阵直接断言错误码，无数据库依赖；目录缺席时 fail-closed。
"""

from __future__ import annotations

import pytest

from iesplan.application.tasks.submissions import _check_prediction_inputs
from iesplan.computation import (
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    PredictorDescriptor,
    build_predictor_registry,
)
from iesplan.core.errors import AppError
from iesplan.tasks import InvalidRequestError


def _doc(*bindings: tuple[str, str, str]) -> dict:
    """手造规范装配文档: bindings 为 (实例, 接口, mode) 三元组。"""
    devices: dict[str, dict] = {}
    for dev, iface, mode in bindings:
        entry: dict = {"mode": mode, "path": f"{dev}_{iface}.csv"}
        if mode == "data_predict":
            entry["target_type"] = "load"
        devices.setdefault(dev, {}).setdefault("predefined_interfaces", {})[iface] = entry
    return {"devices": devices}


def _pred(dev: str, iface: str, alg: str, parameters: dict | None = None) -> dict:
    return {
        "target": {"instance": dev, "interface": iface},
        "algorithm": alg,
        "parameters": parameters if parameters is not None else {},
    }


def _config(*preds: dict) -> dict:
    return {"predictions": list(preds)}


def _naive_registry():
    return build_predictor_registry((NAIVE_PREDICTOR,))


class _LoadOnlyPredictor:
    @property
    def descriptor(self):
        return PredictorDescriptor(
            ref="test.predictor.load-only@1.0.0",
            depends_on_solve_result=False,
            accepts_any_target_type=False,
            target_types=("other_type",),
            needs_history=True,
        )

    def validate_parameters(self, parameters):
        return None


class TestPredictionInputsFreeze:
    def test_empty_both_passes(self) -> None:
        assert _check_prediction_inputs(_doc(), _config(), 1) == 0

    def test_non_predict_bindings_ignored(self) -> None:
        doc = _doc(("d1", "i1", "constant"), ("d1", "i2", "data_repeat"))
        assert _check_prediction_inputs(doc, _config(), 1) == 0

    def test_matching_single_independent_passes(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        assert (
            _check_prediction_inputs(
                doc, _config(_pred("pv1", "power_out", NAIVE_PREDICTOR_REF)), 1,
                predictor_registry=_naive_registry(),
            )
            == 1
        )

    def test_two_independent_pass(self) -> None:
        doc = _doc(
            ("pv1", "power_out", "data_predict"),
            ("pv2", "power_out", "data_predict"),
        )
        cfg = _config(
            _pred("pv1", "power_out", NAIVE_PREDICTOR_REF),
            _pred("pv2", "power_out", NAIVE_PREDICTOR_REF),
        )
        assert _check_prediction_inputs(doc, cfg, 1, predictor_registry=_naive_registry()) == 2

    def test_unsupported_target_type_rejected(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        cfg = _config(_pred("pv1", "power_out", "test.predictor.load-only@1.0.0"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(
                doc, cfg, 1, predictor_registry=build_predictor_registry((_LoadOnlyPredictor(),))
            )
        assert exc_info.value.code == "TASK-REQ-014"

    def test_registry_absent_unavailable(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        with pytest.raises(AppError):
            _check_prediction_inputs(
                doc, _config(_pred("pv1", "power_out", NAIVE_PREDICTOR_REF)), 1
            )

    def test_registry_absent_without_predictions_passes(self) -> None:
        assert _check_prediction_inputs(_doc(), _config(), 1) == 0

    def test_config_target_without_binding_rejected(self) -> None:
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(_doc(), _config(_pred("pv1", "power_out", NAIVE_PREDICTOR_REF)), 1)
        assert exc_info.value.code == "TASK-REQ-010"

    def test_binding_without_config_rejected(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(doc, _config(), 1)
        assert exc_info.value.code == "TASK-REQ-011"

    def test_mismatch_reports_first_sorted_target(self) -> None:
        doc = _doc(("b", "i", "data_predict"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(
                doc,
                _config(
                    _pred("a", "i", NAIVE_PREDICTOR_REF),
                    _pred("b", "i", NAIVE_PREDICTOR_REF),
                ),
                1,
            )
        assert exc_info.value.code == "TASK-REQ-010"
        assert exc_info.value.params["instance"] == "a"
