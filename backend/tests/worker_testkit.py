"""Worker 测试共用工具: 迷你环境构建(用户/项目/版本/快照/数据集/任务/队列)。

供 worker 系测试复用:
- SQLite :memory:(StaticPool 共享连接) + IESPLAN_QUEUE=memory 内存队列;
- 迷你数据集: 4 行 1h CSV(电负荷 1 kW 峰谷电价), 仅作快照绑定存在性之用,
  Worker 不解释其字段与时间轴;
- 迷你方案: 电网 + 电池(与项目内容同构的最小设备清单)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

from sqlalchemy.orm import Session  # noqa: E402

from iesplan.application.projects.content_objects import store_content_object
from iesplan.assembly import (  # noqa: E402
    SCHEMA_ID,
    SCHEMA_VERSION,
    ValidatedAssemblyArtifact,
    ValidationReceipt,
    canonicalize_assembly_doc,
)
from iesplan.config import settings  # noqa: E402
from iesplan.dataset.persistence import Dataset, DatasetFile, DatasetVersion  # noqa: E402
from iesplan.identity.persistence import User  # noqa: E402
from iesplan.project.persistence import Project, ProjectVersion  # noqa: E402
from iesplan.storage import put_object
from iesplan.tasks import queue
from iesplan.tasks.persistence import CalcSnapshot, Task  # noqa: E402

#: 环境序号(保证同库内用户名/项目名唯一, 避免跨环境 UNIQUE 冲突)
_env_seq = 0

#: 计算类任务(与 runner.COMPUTE_TASK_TYPES 一致)
COMPUTE_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty")

#: 迷你数据集 CSV(4 行 1h; 单位: 负荷 kWh/步, 电价 元/kWh, 排放因子 kg/kWh,
#: 午后 ghi=600 W/m² 供光伏算例)
MINI_CSV = (
    "timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,grid_emission_factor\n"
    "2025-01-01 00:00,1.0,0,0,10,0,0.3,0.581\n"
    "2025-01-01 01:00,1.0,0,0,10,0,0.3,0.581\n"
    "2025-01-01 02:00,1.0,0,0,10,600,1.1,0.581\n"
    "2025-01-01 03:00,1.0,0,0,10,600,1.1,0.581\n"
)


def mini_content(
    dataset_version_id: int, config: dict | None = None, devices: list[dict[str, Any]] | None = None,
) -> dict:
    """迷你项目内容(与 project._initial_content 同构; task_params 由快照承载)。"""
    if devices is None:
        devices = [
            {
                "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网",
                # 2.0 电价归 tariff_bindings，设备 params 只保留注册属性。
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0},
            },
            {
                "device_type": "ies.device.battery", "kind": "existing",
                "name": "电池",
                "params": {"capacity_kwh": 2, "rated_power_kw": 2, "initial_soc": 0.5,
                           "min_soc": 0.1, "max_soc": 0.9,
                           "charge_efficiency": 0.95, "discharge_efficiency": 0.95},
            },
        ]
    return {
        "schema_version": 1,
        "language": "zh-CN",
        "unit_system": "si",
        "extensions": {},
        "model": {
            "devices": devices,
            "connections": [],
        },
        "layout": {},
        "dataset_bindings": [{"dataset_version_id": dataset_version_id}],
        "calc_config": {
            "params": {},
            "variables": [],
            "objectives": [],
            "constraints": [],
            "algorithm": None,
            "solver": None,
            "tolerances": {},
            "random_seed": None,
        },
        "applied_commands": {},
    }


#: 机械夹具装配文档的财务引用身份(与回执 finance_snapshot.ref 一致)。
_FIXTURE_FINANCE_OBJECT_ID = "fixture-effective-object"
_FIXTURE_FINANCE_PROFILE_ID = "cn-fixture-demo"


#: 机械夹具依赖锁固定版本(非实时设备目录值；夹具只保证二件套非空、
#: schema 3.0、文本与回执相互一致，不代表真实签发时的内容锁)。
_FIXTURE_DEVICE_LOCK = {
    "ies.device.battery": "2.0.0",
    "ies.device.grid_connection": "2.0.0",
}
_FIXTURE_EQUATION_LOCK = {
    "contract": "ies.modeling.contribution@2.0.0",
    "ast": "ies.equation.ast@2.0.0",
}


def fixture_artifact() -> ValidatedAssemblyArtifact:
    """机械 3.0 装配二件套(不跑装配校验)。

    规范文本由规范化纯函数生成(非校验);回执字段与同一文档一致:
    schema 3.0.0、固定机械依赖锁、财务引用、规划摘要、零阻断诊断。
    调用方(worker 机械测试/计算边界测试)只消费二件套形状,不代表真实签发。
    """
    from iesplan.finance import SCHEMA_EFFECTIVE
    from iesplan.finance import SCHEMA_VERSION as FINANCE_SCHEMA_VERSION

    doc = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "assembly": {"id": "fixture", "name": "机械夹具装配"},
        "project_baseline": {
            "resolution": "1h", "start_year": 2026, "scenario_mode": "single",
        },
        "devices": {
            "d1": {
                "definition": {"id": "ies.device.grid_connection"},
                "asset_origin": "existing",
                "properties": {
                    "max_import_power_kw": {"value": 5000, "unit": "kW"},
                    "max_export_power_kw": {"value": 0, "unit": "kW"},
                },
                "predefined_interfaces": {},
            },
            "d2": {
                "definition": {"id": "ies.device.battery"},
                "asset_origin": "existing",
                "properties": {
                    "capacity_kwh": {"value": 2, "unit": "kWh"},
                    "rated_power_kw": {"value": 2, "unit": "kW"},
                },
                "predefined_interfaces": {},
            },
        },
        "connections": {},
        "finance": {
            "ref": {
                "kind": "object",
                "object_id": _FIXTURE_FINANCE_OBJECT_ID,
                "media_type": "application/yaml",
            },
            "profile_id": _FIXTURE_FINANCE_PROFILE_ID,
        },
        "finance_binding": {
            "instances": {"d1": {"type": "none"}, "d2": {"type": "none"}},
        },
        "tariff_bindings": {},
        "planning": {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": {
                "cap": {
                    "device_ref": "d2",
                    "parameter": "capacity_kwh",
                    "lower_bound": "0",
                    "upper_bound": "1000",
                    "unit": "kWh",
                },
            },
            "constraints": {},
        },
        "outputs": {"series": [], "metrics": []},
        "extensions": {},
    }
    receipt = ValidationReceipt(
        dependencies={
            "devices": dict(_FIXTURE_DEVICE_LOCK),
            "baseline": {
                "resolution": "1h", "start_year": 2026, "scenario_mode": "single",
            },
            "equations": dict(_FIXTURE_EQUATION_LOCK),
        },
        resources={},
        finance_snapshot={
            "ref": {
                "kind": "object",
                "object_id": _FIXTURE_FINANCE_OBJECT_ID,
                "media_type": "application/yaml",
                "path": None,
            },
            "profile_id": _FIXTURE_FINANCE_PROFILE_ID,
            "revision": None,
            "schema": SCHEMA_EFFECTIVE,
            "schema_version": FINANCE_SCHEMA_VERSION,
            "currency": "CNY",
        },
        planning={
            "objective_sense": "minimize", "variables": ["cap"], "constraints": [],
        },
        diagnostics=(),
    )
    return ValidatedAssemblyArtifact(
        canonical_text=canonicalize_assembly_doc(doc), receipt=receipt
    )


def setup_environment(
    db: Session,
    tmp_path: Path,
    *,
    task_type: str = "calc",
    config: dict[str, Any] | None = None,
    status: str = "queued",
    with_dataset: bool = True,
    devices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构建迷你任务环境, 返回各实体引用字典。

    - 数据集版本(4 行 1h CSV 对象)+ 项目版本内容对象;
    - 计算快照(calc_config_snapshot 含 task_params = config);
    - 任务(status 缺省 queued; 计算类绑定快照)并已入队;
    - devices: 自定义设备清单(缺省: 电网 + 电池, 见 mini_content)。
    """
    settings.data_dir = tmp_path
    settings.storage_min_free_bytes = 0  # 测试环境关闭存储门禁
    global _env_seq
    _env_seq += 1
    user = User(username=f"worker-tester-{_env_seq}", display_name="测试用户")
    db.add(user)
    db.flush()
    project = Project(name=f"测试项目-{_env_seq}-{task_type}", owner_id=user.id,
                      created_by=user.id, status="active",
                      baseline_resolution="1h", baseline_start_year=2026,
                      baseline_scenario_mode="single",
                      )
    db.add(project)
    db.flush()

    dver_id: int | None = None
    if with_dataset:
        csv_bytes = MINI_CSV.encode("utf-8")
        obj = put_object(db, csv_bytes, "text/csv", "dataset_file", purpose="dataset_data",
                                 actor_id=user.id)
        dset = Dataset(name=f"迷你数据集-{_env_seq}", status="published", created_by=user.id)
        db.add(dset)
        db.flush()
        dver = DatasetVersion(
            dataset_id=dset.id, version_no=1, timeline="hourly", resolution="1h",
            fixed_utc_offset_minutes=480, fields={}, units={},
            created_by=user.id,
        )
        db.add(dver)
        db.flush()
        dver_id = dver.id
        db.add(DatasetFile(
            dataset_version_id=dver.id, object_id=obj.id, file_kind="data",
            format="csv", row_count=4, size_bytes=len(csv_bytes),
        ))

    content = mini_content(dver_id if dver_id is not None else 0, config=config, devices=devices)
    content_object_id = store_content_object(db, content)
    version = ProjectVersion(
        project_id=project.id, version_no=1, name="v1", reason="snapshot_freeze",
        baseline_resolution="1h", baseline_start_year=2026,
        baseline_scenario_mode="single",
        currency="CNY", schema_version=1,
        content_object_id=content_object_id, created_by=user.id,
    )
    db.add(version)
    db.flush()
    project.current_version_id = version.id

    snapshot = None
    if task_type in COMPUTE_TYPES:
        # 机械快照行(Worker 队列/租约 mechanics 用): 保存非空且内部一致的
        # 3.0 二件套形状(不跑装配校验);真实签发只走任务提交闸门。
        artifact = fixture_artifact()
        snapshot = CalcSnapshot(
            project_version_id=version.id,
            dataset_version_ids=[dver_id] if dver_id is not None else [],
            calc_config_snapshot={"params": {}, "task_params": dict(config or {}),
                                  "random_seed": None},
            program_version="0.1.0",
            extension_versions={},
            random_seed=int(config.get("seed", 42)) if config else 42,
            schema_version="3.0.0",
            result_adapter="ies.result-adapter.highs@1.0.0",
            executor="ies.executor.local_process@1.0.0",
            canonical_assembly_text=artifact.canonical_text,
            assembly_receipt=artifact.receipt.to_dict(),
            created_by=user.id,
        )
        db.add(snapshot)
        db.flush()

    task = Task(
        project_id=project.id, type=task_type, status=status,
        calc_snapshot_id=snapshot.id if snapshot is not None else None,
        requested_by=user.id,
    )
    db.add(task)
    db.flush()
    if status == "queued":
        pool = "compute" if task_type in COMPUTE_TYPES else "io"
        queue.enqueue(task.id, pool, task_type=task_type, snapshot_id=task.calc_snapshot_id)
    db.commit()
    return {
        "user": user, "project": project, "version": version,
        "snapshot": snapshot, "dataset_version_id": dver_id,
        "task": task, "content": content,
    }
