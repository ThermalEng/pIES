"""B5-1 预测输入冻结核对(提交闸门): 配置 predictions 与装配 data_predict 绑定双向匹配 + 多目标收敛必填。

纯函数矩阵直接断言错误码，无数据库依赖。
"""

from __future__ import annotations

import pytest

from iesplan.application.tasks.submissions import _check_prediction_inputs
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


def _pred(dev: str, iface: str) -> dict:
    return {
        "target": {"instance": dev, "interface": iface},
        "algorithm": "ies.predictor.naive@1.0.0",
        "parameters": {},
    }


def _config(*preds: dict, convergence: dict | None = None) -> dict:
    return {"predictions": list(preds), "convergence": convergence}


class TestPredictionInputsFreeze:
    def test_empty_both_passes(self) -> None:
        assert _check_prediction_inputs(_doc(), _config(), 1) == 0

    def test_non_predict_bindings_ignored(self) -> None:
        doc = _doc(("d1", "i1", "constant"), ("d1", "i2", "data_repeat"))
        assert _check_prediction_inputs(doc, _config(), 1) == 0

    def test_matching_single_passes_without_convergence(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        assert _check_prediction_inputs(doc, _config(_pred("pv1", "power_out")), 1) == 1

    def test_config_target_without_binding_rejected(self) -> None:
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(_doc(), _config(_pred("pv1", "power_out")), 1)
        assert exc_info.value.code == "TASK-REQ-010"

    def test_binding_without_config_rejected(self) -> None:
        doc = _doc(("pv1", "power_out", "data_predict"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(doc, _config(), 1)
        assert exc_info.value.code == "TASK-REQ-011"

    def test_two_bindings_require_convergence(self) -> None:
        doc = _doc(
            ("pv1", "power_out", "data_predict"),
            ("pv2", "power_out", "data_predict"),
        )
        cfg = _config(_pred("pv1", "power_out"), _pred("pv2", "power_out"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(doc, cfg, 1)
        assert exc_info.value.code == "TASK-REQ-012"
        cfg["convergence"] = {"tolerance": 0.001, "max_iterations": 50}
        assert _check_prediction_inputs(doc, cfg, 1) == 2

    def test_mismatch_reports_first_sorted_target(self) -> None:
        doc = _doc(("b", "i", "data_predict"))
        with pytest.raises(InvalidRequestError) as exc_info:
            _check_prediction_inputs(doc, _config(_pred("a", "i"), _pred("b", "i")), 1)
        assert exc_info.value.code == "TASK-REQ-010"
        assert exc_info.value.params["instance"] == "a"


#: 提交接线证明见 test_worker_compute_chain
#: .TestComputeChainEndToEnd.test_prediction_required_fails_with_clear_reason
#: （无绑定目标 predictions 提交即 400 TASK-REQ-010）；Rule B/C 的 API
#: 端到端随 B5-2（届时必须构造真实预测项目）补齐。
