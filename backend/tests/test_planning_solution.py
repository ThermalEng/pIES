"""规划解投影测试(S3-A 收口，S3-B 复用领域投影)。

覆盖领域 planning_solution_params/_planning_solution: 冻结名单投影、
同一快照正文绑定附着、封存值/单位原样、双实例绑定、非名单变量跳过；
业务缺席(无证据包、无 compute 块、metrics-only 无 solution 块)返回
None。内部可信形态不重验，故无损坏检测用例；DB 行不伪造，材料以
同形存根传入。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from iesplan import results as results_domain
from iesplan.application.results.writes import _planning_solution

NAMES = ["hp1_cap", "hp2_cap"]
BINDINGS = {
    "hp1_cap": {"device_ref": "hp1", "parameter": "rated_heat_kw", "unit": "kW"},
    "hp2_cap": {"device_ref": "hp2", "parameter": "rated_heat_kw", "unit": "kW"},
}
VARIABLES = [
    {"name": "hp1_cap", "value": 50.0, "unit": "kW"},
    {"name": "grid1__electricity_in__t0", "value": 10.0, "unit": "kW"},
    {"name": "hp2_cap", "value": 200.0, "unit": "kW"},
]


def _materials(outputs: dict) -> tuple:
    task = SimpleNamespace(id=5, calc_snapshot_id=7)
    package = SimpleNamespace(id=9)
    content = {"compute_result": {"outputs": outputs}}
    snapshot = SimpleNamespace(
        project_version_id=86,
        dataset_version_ids=[11],
        assembly_receipt={"planning": {"variables": list(NAMES)}},
        canonical_assembly_text=json.dumps({"planning": {"variables": dict(BINDINGS)}}),
    )
    return task, package, content, snapshot


def test_projects_dual_instance_true_values() -> None:
    params = results_domain.planning_solution_params(VARIABLES, NAMES, BINDINGS)
    assert params == [
        {"variable": "hp1_cap", "device_ref": "hp1", "parameter": "rated_heat_kw", "value": 50.0, "unit": "kW"},
        {"variable": "hp2_cap", "device_ref": "hp2", "parameter": "rated_heat_kw", "value": 200.0, "unit": "kW"},
    ]


def test_solution_view_carries_refs() -> None:
    task, package, content, snapshot = _materials({"solution": {"variables": VARIABLES}})
    out = _planning_solution(task, package, content, snapshot)
    assert out is not None
    assert out["solution_id"] == 0
    assert out["single_solution"] is True
    assert [p["variable"] for p in out["params"]] == ["hp1_cap", "hp2_cap"]
    assert out["refs"] == {
        "task_id": 5,
        "calc_snapshot_id": 7,
        "project_version_id": 86,
        "evidence_package_id": 9,
        "dataset_version_ids": [11],
    }


def test_no_package_is_absence() -> None:
    task, _, content, snapshot = _materials({"solution": {"variables": VARIABLES}})
    assert _planning_solution(task, None, content, snapshot) is None


def test_metrics_only_is_absence() -> None:
    task, package, content, snapshot = _materials({"metrics": {}})
    assert _planning_solution(task, package, content, snapshot) is None


def test_no_compute_block_is_absence() -> None:
    task, package, _, snapshot = _materials({})
    assert _planning_solution(task, package, {"metrics": {}}, snapshot) is None
