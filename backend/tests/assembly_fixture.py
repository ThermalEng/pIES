"""装配计算提交公共夹具(机械测试用): 模型电池设备 + 最小规划修订。

计算任务提交闸门只消费冻结版本内容钉选的精确引用(effective_finance /
planning_config)，不再接受 assembly.set_planning 草稿副本(已删除)。
本模块提供各测试复用的最小公共搭建：经模型公共 API 创建单电池设备，
经规划公共用例保存最小规划修订(常量目标 + 以该设备为 device_ref 的
单变量)。真实项目按真实拓扑与真实规划声明。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

#: 机械夹具电池设备(无预定义接口、单双向端口，无需连接即可通过装配)。
FIXTURE_BATTERY_TYPE = "ies.device.battery"
FIXTURE_BATTERY_NAME = "bat1"
FIXTURE_BATTERY_PARAMS: dict[str, Any] = {
    "capacity_kwh": 200,
    "rated_power_kw": 100,
}


def declare_compute_device(
    client: TestClient, pid: int, headers: dict[str, str]
) -> str:
    """经模型公共 API 创建夹具电池设备，返回装配实例引用(``d{id}``)。

    设备行 id 由模型域分配；装配构造器按 ``d{id}`` 规则派生实例引用，
    finance_binding 与规划变量须使用同一引用。
    """
    resp = client.post(
        f"/api/projects/{pid}/model/devices",
        json={
            "device_type": FIXTURE_BATTERY_TYPE,
            "name": FIXTURE_BATTERY_NAME,
            "params": dict(FIXTURE_BATTERY_PARAMS),
            "position": {"x": 1.0, "y": 1.0},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return f"d{resp.json()['device']['id']}"


def save_compute_planning(
    db: Session, pid: int, user_id: int, device_ref: str
) -> int:
    """经规划公共用例保存最小规划修订，返回 revision。

    目标为常量(无标识符)；单变量绑定给定设备实例的额定容量；
    公共 PlanningConfig 契约要求至少一个变量。调用方须已引用财务
    Profile(规划保存要求有效快照先行)。
    """
    from iesplan.application import configuration as config_cases

    _, revision = config_cases.save_planning_config(
        db,
        pid,
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "cap": {
                    "device_ref": device_ref,
                    "parameter": "capacity_kwh",
                    "lower_bound": "0",
                    "upper_bound": "1000",
                    "unit": "kWh",
                },
            },
            "constraints": {},
        },
        None,
        user_id,
    )
    return revision
