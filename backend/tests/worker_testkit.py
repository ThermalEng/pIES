"""Worker 测试共用工具: 迷你环境构建(用户/项目/版本/快照/数据集/任务/队列)。

供 tests/test_worker_lease.py 与 tests/test_worker_runner.py 复用:
- SQLite :memory:(StaticPool 共享连接) + IESPLAN_QUEUE=memory 内存队列;
- 迷你数据集: 4 行 1h CSV(电负荷 1 kW 峰谷电价), 行数 < 标准年步数,
  runner._build_axis 按行数构造迷你时间轴;
- 迷你方案: 电网 + 电池(同 test_eval_run 迷你算例, 便于手算校验)。
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

from sqlalchemy.orm import Session  # noqa: E402

from iesplan.config import settings  # noqa: E402
from iesplan.models.calc import CalcSnapshot, Task  # noqa: E402
from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.models.project import Project, ProjectVersion  # noqa: E402
from iesplan.services import objects, queue  # noqa: E402
from iesplan.services import project as project_service

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
                "id": 1, "device_type": "ies.device.grid_connection", "kind": "existing",
                "name": "电网",
                "params": {"max_import_power_kw": 5000, "max_export_power_kw": 0,
                           "export_tariff": 0.35, "demand_charge": 0},
            },
            {
                "id": 2, "device_type": "ies.device.battery", "kind": "existing",
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
            "ports": [],
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
    settings.storage_min_free_bytes = 0
    global _env_seq
    _env_seq += 1
    user = User(username=f"worker-tester-{_env_seq}", display_name="测试用户")
    db.add(user)
    db.flush()
    project = Project(name=f"测试项目-{_env_seq}-{task_type}", owner_id=user.id,
                      created_by=user.id, status="active")
    db.add(project)
    db.flush()

    dver_id: int | None = None
    if with_dataset:
        csv_bytes = MINI_CSV.encode("utf-8")
        obj = objects.put_object(db, csv_bytes, "text/csv", "dataset_file", purpose="dataset_data",
                                 actor_id=user.id)
        dset = Dataset(name=f"迷你数据集-{_env_seq}", status="published", created_by=user.id)
        db.add(dset)
        db.flush()
        dver = DatasetVersion(
            dataset_id=dset.id, version_no=1, timeline="hourly", resolution="1h",
            fixed_utc_offset_minutes=480, fields={}, units={},
            content_hash=sha256(csv_bytes).hexdigest(), created_by=user.id,
        )
        db.add(dver)
        db.flush()
        dver_id = dver.id
        db.add(DatasetFile(
            dataset_version_id=dver.id, object_id=obj.id, file_kind="data",
            format="csv", row_count=4, size_bytes=len(csv_bytes),
        ))

    content = mini_content(dver_id if dver_id is not None else 0, config=config, devices=devices)
    content_hash = project_service.store_content_object(db, content)
    version = ProjectVersion(
        project_id=project.id, version_no=1, name="v1", reason="snapshot_freeze",
        fixed_utc_offset_minutes=480, currency="CNY", schema_version=1,
        content_hash=content_hash, created_by=user.id,
    )
    db.add(version)
    db.flush()
    project.current_version_id = version.id

    snapshot = None
    if task_type in COMPUTE_TYPES:
        snapshot = CalcSnapshot(
            project_version_id=version.id,
            dataset_version_ids=[dver_id] if dver_id is not None else [],
            calc_config_snapshot={"params": {}, "task_params": dict(config or {}),
                                  "random_seed": None},
            program_version="0.1.0",
            extension_versions={},
            random_seed=int(config.get("seed", 42)) if config else 42,
            tolerances={},
            content_hash=sha256(f"snapshot-{task_type}".encode()).hexdigest(),
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
