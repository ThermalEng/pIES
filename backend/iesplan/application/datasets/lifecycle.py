"""数据集生命周期用例(application/datasets)。

数据集全生命周期编排（旧 ``services.dataset`` 已删除，权威 CSV 规则与
persistence 归 dataset 域）。CSV 解析/校验纯函数直用 dataset 域唯一实现
（``iesplan.dataset.tables``，经领域公开门面）。

事务：写用例顶层函数拥有提交/回滚（``db.commit`` 收尾，失败 ``db.rollback``）；
内部步骤只 ``flush``，由顶层统一。读用例不提交事务。

调用方向：``api → application.datasets.lifecycle → {dataset, project,
identity, storage} 域公开门面``；不导入 ORM、不导入其他域内部模块、
不调用 ``services.*``。
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from iesplan import dataset as dataset_domain
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.application.datasets.quotas import check_upload_quota
from iesplan.application.projects.authorization import ensure_access
from iesplan.core.diagnostics import (
    DATA_COL_UNIT_UNKNOWN,
    PARAM_UNIT_MISMATCH,
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.core.timeaxis import RESOLUTIONS, TimeAxis, build_axis
from iesplan.dataset import (
    DEFAULT_SOURCE_CATEGORY,
    SAMPLE_LICENSE,
    STANDARD_FIELDS,
    TIMELINE_MAP,
    TIMESTAMP_COL,
    DataValidationError,
    build_quality_report,
    get_template,
    normalized_to_csv_bytes,
    parse_csv,
    unit_matches,
    validate_dataset,
)
from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.identity.contracts import UserRecord
from iesplan.storage import (
    RefInfo,
    add_ref,
    get_object,
    object_info,
)
from iesplan.storage import (
    put_object as _storage_put_object,
)

#: 数据对象媒体类型
DATA_MEDIA_TYPE: str = "text/csv; charset=utf-8"
METADATA_MEDIA_TYPE: str = "application/json"

# ---------------------------------------------------------------------------
# 对象存储(薄封装, 委托 storage 公开门面)
# ---------------------------------------------------------------------------


def put_object(
    db: Session,
    data: bytes,
    media_type: str,
    *,
    source_category: str = "dataset",
) -> dict:
    """写入新对象(委托 storage 门面; 每次写入新建对象, 按对象 id 寻址)。"""
    return _storage_put_object(db, data, media_type, source_category=source_category)


def get_object_bytes(db: Session, object_id: int) -> bytes:
    """按公开对象 ID 读取对象字节(不做内容复核, 委托 storage 门面)。"""
    return get_object(db, object_id)


def add_object_ref(
    db: Session,
    obj: object,
    ref_type: str,
    ref_entity_type: str,
    ref_entity_id: int,
    purpose: str | None = None,
) -> RefInfo | None:
    """建立对象引用(接受 ObjectHandle 或元数据 dict, 委托门面)。"""
    object_id = obj["id"] if isinstance(obj, dict) else obj.id
    return add_ref(
        db, object_id, ref_type, ref_entity_id, ref_entity_type=ref_entity_type, purpose=purpose
    )


# ---------------------------------------------------------------------------
# 数据集 / 版本用例
# ---------------------------------------------------------------------------


def default_user(db: Session) -> UserRecord:
    """返回系统操作者(admin); 不存在则创建(认证接入前的占位实现, 经 identity 域)。"""
    user = identity_domain.get_user_by_username(db, "admin")
    if user is None or user.status != "active":
        user = identity_domain.create_user(
            db,
            username="admin",
            display_name="系统管理员",
            is_system=True,
        )
    return user


def _create_dataset(
    db: Session,
    project_id: int,
    name: str,
    source_category: str | None = None,
    license: str | None = None,
    provenance: dict | None = None,
    *,
    user_id: int | None = None,
    description: str | None = None,
) -> DatasetRecord:
    """创建数据集元数据(经 dataset 域 repository，只 flush，不提交)。

    注: source_category/provenance 按数据模型归属版本, 此处接受参数
    仅为接口完整与默认值透传; 实际入库发生在版本上传。
    """
    if project_domain.get_project(db, project_id) is None:
        raise NotFoundError(params={"entity_type": "project", "entity_id": project_id})
    actor_id = user_id if user_id is not None else default_user(db).id
    try:
        return dataset_domain.create_dataset(
            db,
            name=name,
            created_by=actor_id,
            project_id=project_id,
            description=description,
            default_license=license,
            # 透传默认溯源(版本创建时若未显式给出则继承)
            source_category=source_category if provenance else None,
            default_provenance=dict(provenance) if provenance else None,
        )
    except DatasetConflictError as exc:
        raise ConflictError(
            message_key="ies.error.duplicate_name",
            params={"entity_type": "dataset", "name": name, "project_id": project_id},
        ) from exc


def create_dataset(
    db: Session,
    project_id: int,
    name: str,
    source_category: str | None = None,
    license: str | None = None,
    provenance: dict | None = None,
    *,
    user_id: int | None = None,
    description: str | None = None,
) -> DatasetRecord:
    """事务型创建数据集；application 层统一提交或回滚。"""
    try:
        dataset = _create_dataset(
            db,
            project_id,
            name,
            source_category=source_category,
            license=license,
            provenance=provenance,
            user_id=user_id,
            description=description,
        )
        db.commit()
        return dataset
    except Exception:
        db.rollback()
        raise


def _build_fields_info(
    df: pd.DataFrame,
    declared: dict | None,
) -> tuple[dict, dict, list[Diagnostic]]:
    """合并字段定义与单位(01 §5.2 fields/units), 校验声明单位一致性。"""
    declared = dict(declared or {})
    fields: dict = {
        TIMESTAMP_COL: {
            "type": "datetime",
            "unit": "ISO8601",
            "description_zh": "时间戳(项目本地时间)",
            "description_en": "Timestamp (project local time)",
        }
    }
    units: dict = {TIMESTAMP_COL: "ISO8601"}
    diags: list[Diagnostic] = []
    for col in df.columns:
        if col == TIMESTAMP_COL:
            continue
        spec = STANDARD_FIELDS.get(col)
        decl = declared.get(col)
        unit = ""
        if isinstance(decl, dict):
            unit = str(decl.get("unit", "")).strip()
        elif isinstance(decl, str):
            unit = decl.strip()
        if spec is not None:
            if unit and not unit_matches(unit, spec.unit):
                diags.append(
                    make_diag(
                        PARAM_UNIT_MISMATCH,
                        severity=SEVERITY_ERROR,
                        blocking=True,
                        params={"field": col, "expected": spec.unit, "actual": unit},
                        location=_field_location(col),
                    )
                )
            unit = unit or spec.unit
            fields[col] = {
                "type": "float",
                "unit": unit,
                "description_zh": spec.name_zh,
                "description_en": spec.name_en,
            }
        else:
            if not unit:
                diags.append(
                    make_diag(
                        DATA_COL_UNIT_UNKNOWN,
                        severity="warning",
                        params={"column": col, "hint": "非标准字段, 请补充单位声明"},
                        location=_field_location(col),
                    )
                )
            unit = unit or "unknown"
            fields[col] = {"type": "float", "unit": unit, "description_zh": col, "description_en": col}
        units[col] = unit
    return fields, units, diags


def _field_location(field: str, rows: list[int] | None = None) -> dict:
    """构造字段定位字典（与 dataset 域 tables 同构，版本写入路径本地复用）。"""
    loc = {"object_type": "time_series", "object_id": "", "field": field}
    if rows:
        loc["row"] = rows[:5]
    return loc


def _commit_version(
    db: Session,
    dataset: DatasetRecord,
    axis: TimeAxis,
    normalized_df: pd.DataFrame,
    diags: list[Diagnostic],
    declared_fields: dict | None,
    meta: dict,
    actor_id: int | None = None,
    *,
    canonical_csv: bytes | None = None,
    row_count: int | None = None,
) -> DatasetVersionRecord:
    """校验通过后执行版本写入(对象 + 版本行 + 文件行 + 引用, 只 flush，不提交)。

    canonical_csv: 规范化器产出的规范表格字节; 提供时直接落盘(上传路径:
    与手写 CSV 同一内容 → 同一规范字节), 缺省由 DataFrame 重新序列化
    (内置样例路径, 无元数据头)。
    row_count: 实际数据行数; 缺省用 axis.n(标准年步数)。
    """
    fields_info, units, unit_diags = _build_fields_info(normalized_df, declared_fields)
    all_diags = list(diags) + unit_diags
    if any(d.blocking for d in all_diags):
        raise DataValidationError(all_diags)

    n_rows = row_count if row_count is not None else axis.n
    if canonical_csv is None:
        canonical_csv = normalized_to_csv_bytes(normalized_df)
    quality_report = build_quality_report(
        axis, normalized_df, all_diags,
    )

    source_category = (
        meta.get("source_category") or getattr(dataset, "source_category", None) or DEFAULT_SOURCE_CATEGORY
    )
    provenance = dict(meta.get("provenance") or {})
    provenance.setdefault("source_category", source_category)
    license = meta.get("license") or dataset.default_license
    created_reason = meta.get("created_reason") or "upload"

    # 数据本体先落盘为对象存储对象; 版本行只持有不可变版本号,
    # 历史定位使用 (dataset_id, version_no), 规范字节经 dataset_files 对象引用读取。
    # version_no 由 dataset 域分配(单调递增, 并发冲突抛 DatasetConflictError)。
    obj_data = put_object(db, canonical_csv, DATA_MEDIA_TYPE, source_category=source_category)
    version = dataset_domain.create_version(
        db,
        dataset_id=dataset.id,
        timeline=TIMELINE_MAP[axis.resolution],
        fixed_utc_offset_minutes=axis.utc_offset_minutes,
        fields=fields_info,
        units=units,
        created_by=actor_id if actor_id is not None else default_user(db).id,
        resolution=axis.resolution,
        quality_report=quality_report,
        provenance=provenance,
        license=license,
        created_reason=created_reason,
    )
    metadata_json = json.dumps(
        {
            "dataset_id": dataset.id,
            "dataset_version_id": version.id,
            "version_no": version.version_no,
            "resolution": axis.resolution,
            "timeline": TIMELINE_MAP[axis.resolution],
            "fixed_utc_offset_minutes": axis.utc_offset_minutes,
            "row_count": n_rows,
            "fields": fields_info,
            "units": units,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    obj_meta = put_object(db, metadata_json, METADATA_MEDIA_TYPE, source_category=source_category)

    file_data = dataset_domain.add_file(
        db,
        dataset_version_id=version.id,
        object_id=obj_data.id,
        file_kind="data",
        format="csv",
        row_count=n_rows,
        size_bytes=len(canonical_csv),
    )
    file_meta = dataset_domain.add_file(
        db,
        dataset_version_id=version.id,
        object_id=obj_meta.id,
        file_kind="metadata",
        format="json",
        row_count=0,
        size_bytes=len(metadata_json),
    )
    add_object_ref(
        db, {"id": obj_data.id}, "dataset_file", "dataset_files", file_data.id,
        purpose="数据集版本数据本体",
    )
    add_object_ref(
        db, {"id": obj_meta.id}, "dataset_file", "dataset_files", file_meta.id,
        purpose="数据集版本元数据",
    )
    db.flush()
    return version


def _upload_dataset_version(
    db: Session,
    dataset_id: int,
    resolution: str,
    utc_offset_minutes: int,
    fields: dict,
    data_bytes: bytes,
    meta: dict,
    *,
    user_id: int | None = None,
) -> DatasetVersionRecord:
    """在用户上传入口校验并保存数据集版本（只 flush，不提交）。

    CSV 在这里完成一次表头、时间轴、数值、范围和声明单位校验。保存后的内部
    读取与交接信任该结果，不再绑定设备文件、重复校验内容或比较摘要。

    参数:
        db: 数据库会话。
        dataset_id: 数据集 id。
        resolution: '15min' | '30min' | '1h'。
        utc_offset_minutes: 固定 UTC 偏移(分钟)。
        fields: 字段描述 {"e_load": {"unit": "kWh", ...}}(可为空字典自动推断)。
        data_bytes: CSV 数据字节。
        meta: 元信息 {source_category, license, provenance, created_reason}。
    返回:
        新建的 DatasetVersion。
    异常:
        DataValidationError: 存在阻断性诊断(携带诊断明细)。
        NotFoundError: 数据集不存在。
        ConflictError: 数据集已 deprecated, 禁止新建版本。
    """
    dataset = dataset_domain.get_dataset(db, dataset_id)
    if dataset is None:
        raise NotFoundError(params={"entity_type": "dataset", "entity_id": dataset_id})
    if dataset.status == "deprecated":
        raise ConflictError(
            message_key="ies.error.dataset_deprecated",
            params={"dataset_id": dataset_id},
        )

    rows, parse_diags = parse_csv(data_bytes, resolution)
    frame = pd.DataFrame(rows)
    axis, normalized, validation_diags = validate_dataset(
        frame, resolution, utc_offset_minutes
    )
    diags = [*parse_diags, *validation_diags]
    for name, declaration in fields.items():
        if name not in STANDARD_FIELDS or not isinstance(declaration, dict):
            continue
        declared_unit = declaration.get("unit")
        expected_unit = STANDARD_FIELDS[name].unit
        if isinstance(declared_unit, str) and not unit_matches(declared_unit, expected_unit):
            diags.append(make_diag(
                DATA_COL_UNIT_UNKNOWN,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"column": name, "unit": declared_unit, "expected": expected_unit},
                location={"object_type": "time_series", "field": name},
            ))
    if any(d.blocking for d in diags):
        raise DataValidationError(diags)
    return _commit_version(
        db, dataset, axis, normalized, diags, fields, meta,
        actor_id=user_id,
        canonical_csv=normalized_to_csv_bytes(normalized),
        row_count=len(normalized),
    )


def upload_dataset_version(
    db: Session,
    dataset_id: int,
    resolution: str,
    utc_offset_minutes: int,
    fields: dict,
    data_bytes: bytes,
    meta: dict,
    *,
    user_id: int | None = None,
) -> DatasetVersionRecord:
    """事务型上传数据集版本；application 层统一提交或回滚。"""
    try:
        version = _upload_dataset_version(
            db, dataset_id, resolution, utc_offset_minutes, fields, data_bytes, meta,
            user_id=user_id,
        )
        db.commit()
        return version
    except Exception:
        db.rollback()
        raise


def get_dataset(db: Session, dataset_id: int) -> DatasetRecord | None:
    """按 id 获取数据集(不存在返回 None)。"""
    return dataset_domain.get_dataset(db, dataset_id)


def require_project(db: Session, project_id: int) -> None:
    """校验项目存在且未删除, 否则 NotFoundError(与 project 域口径一致)。"""
    if project_domain.get_project(db, project_id) is None:
        raise NotFoundError(params={"entity_type": "project", "entity_id": project_id})


def version_files_summary(db: Session, version_id: int) -> list[dict]:
    """版本文件摘要(不含对象内容)。"""
    files: list[dict] = []
    for f in dataset_domain.list_files(db, version_id):
        files.append(
            {
                "file_kind": f.file_kind,
                "format": f.format,
                "row_count": f.row_count,
                "size_bytes": f.size_bytes,
            }
        )
    return files


def list_dataset_versions(db: Session, dataset_id: int) -> list[DatasetVersionRecord]:
    """数据集版本列表(新版本在前)。"""
    if dataset_domain.get_dataset(db, dataset_id) is None:
        raise NotFoundError(params={"entity_type": "dataset", "entity_id": dataset_id})
    return dataset_domain.list_versions(db, dataset_id)


def get_dataset_version(
    db: Session,
    dataset_id: int,
    version_no: int | None = None,
) -> dict:
    """获取版本及其数据引用(01 §5.2/5.3)。

    参数:
        dataset_id: 数据集 id。
        version_no: 版本号; None 取最新版本。
    返回:
        {"version": DatasetVersionRecord, "files": [{file_kind, format, row_count,
        size_bytes, media_type}], "data": 汇总引用}。
    """
    if version_no is None:
        version = dataset_domain.get_latest_version(db, dataset_id)
    else:
        version = dataset_domain.get_version_by_no(db, dataset_id, version_no)
    if version is None:
        raise NotFoundError(
            params={"entity_type": "dataset_version", "dataset_id": dataset_id, "version_no": version_no}
        )
    files: list[dict] = []
    for f in dataset_domain.list_files(db, version.id):
        try:
            obj = object_info(db, f.object_id)
        except NotFoundError:
            obj = None
        files.append(
            {
                "id": f.id,
                "object_id": f.object_id,
                "file_kind": f.file_kind,
                "format": f.format,
                "row_count": f.row_count,
                "size_bytes": f.size_bytes,
                "media_type": obj["media_type"] if obj else None,
            }
        )
    data_file = next((f for f in files if f["file_kind"] == "data"), None)
    return {
        "version": version,
        "files": files,
        "data": {
            **(
                {}
                if data_file is None
                else {"row_count": data_file["row_count"], "size_bytes": data_file["size_bytes"]}
            ),
        },
    }


def list_datasets_with_latest(db: Session, project_id: int) -> list[dict]:
    """项目数据集列表, 附带最新版本摘要(创建时间升序; 仅本项目, 不含共享集)。"""
    datasets = sorted(
        (d for d in dataset_domain.list_datasets(db, project_id) if d.project_id == project_id),
        key=lambda d: (d.created_at or "", d.id),
    )
    out: list[dict] = []
    for ds in datasets:
        out.append({"dataset": ds, "latest_version": dataset_domain.get_latest_version(db, ds.id)})
    return out


# ---------------------------------------------------------------------------
# 内置样例数据(REQ-DATA-003)
# ---------------------------------------------------------------------------


def _sample_params(region: str) -> dict:
    """内置样例区域参数(未知区域回退上海)。"""
    table: dict[str, dict] = {
        "shanghai": {
            "t_base": 17.0,
            "t_amp": 11.0,
            "ghi_summer": 1000.0,
            "ghi_winter": 550.0,
            "name": "上海",
        },
        "beijing": {"t_base": 12.5, "t_amp": 15.0, "ghi_summer": 1100.0, "ghi_winter": 420.0, "name": "北京"},
        "guangzhou": {
            "t_base": 22.0,
            "t_amp": 8.0,
            "ghi_summer": 1050.0,
            "ghi_winter": 700.0,
            "name": "广州",
        },
    }
    return table.get(region, table["shanghai"])


def _generate_sample_rows(axis: TimeAxis, region: str, rng: np.random.Generator) -> list[dict]:
    """确定性生成 365 天合成数据(季节 + 日模式, file-formats §设备数据 CSV + domain-model §数据集)。

    含: 电/热/冷负荷、环境温度、水平总辐照、分时电价、电网排放因子。
    全部计算只依赖 (hour_of_year, day_of_year, season) 与固定顺序的 rng 采样,
    同一种子结果完全一致。
    """
    p = _sample_params(region)
    n = axis.n
    hour = axis.hour_of_year % 24
    doy = axis.day_of_year
    season = axis.season
    weekday = (doy % 7) < 5  # 周一至周五
    wd = np.where(weekday, 1.08, 0.86)

    # 环境温度: 年周期 + 日周期 + 噪声
    t = (
        p["t_base"]
        - p["t_amp"] * np.cos(2 * np.pi * (doy - 15) / 365)
        + 3.2 * np.sin(2 * np.pi * (hour - 14) / 24)
        + rng.normal(0.0, 1.2, n)
    )
    # 水平总辐照: 日出日落包络 + 云系数(仅白天非零)
    daylight = np.maximum(0.0, np.sin(np.pi * np.clip((hour - 6.0) / 12.0, 0.0, 1.0))) ** 1.2
    gmax = np.where(
        season == 2,
        p["ghi_summer"],
        np.where(
            season == 3,
            p["ghi_summer"] * 0.85,
            np.where(season == 1, p["ghi_summer"] * 0.9, p["ghi_winter"]),
        ),
    )
    ghi = np.maximum(0.0, gmax * daylight * (0.75 + 0.25 * rng.uniform(size=n)))
    # 电负荷: 早晚双峰 + 季节(夏高冬中) + 周内差异
    hour_curve = (
        1.15 * np.exp(-((hour - 19.0) ** 2) / (2 * 3.5**2))
        + 0.90 * np.exp(-((hour - 9.0) ** 2) / (2 * 2.5**2))
        + 0.55 * np.exp(-((hour - 23.0) ** 2) / 8.0)
        + 0.45 * np.exp(-((hour - 4.0) ** 2) / 6.0)
    )
    season_elec = np.where(season == 2, 1.22, np.where(season == 0, 1.05, 0.90))
    e_load = np.maximum(0.0, 780.0 * wd * season_elec * hour_curve * (0.96 + 0.08 * rng.uniform(size=n)))
    # 热负荷: 冬季主导, 早晚高峰
    winter_w = np.clip(np.cos(2 * np.pi * (doy - 15) / 365) + 0.35, 0.0, 1.3)
    heat_curve = 0.75 + 0.6 * np.exp(-((hour - 21.0) ** 2) / 10.0) + 0.5 * np.exp(-((hour - 7.0) ** 2) / 8.0)
    h_load = np.maximum(0.0, 520.0 * winter_w * heat_curve * wd * (0.95 + 0.1 * rng.uniform(size=n)))
    # 冷负荷: 夏季主导, 午后峰值
    summer_w = np.clip(np.cos(2 * np.pi * (doy - 205) / 365) + 0.4, 0.0, 1.3)
    cool_curve = 0.5 + 0.9 * np.exp(-((hour - 15.0) ** 2) / (2 * 3.0**2))
    c_load = np.maximum(0.0, 460.0 * summer_w * cool_curve * wd * (0.95 + 0.1 * rng.uniform(size=n)))
    # 分时电价: 峰(10-12/18-21) 谷(23-7) 平; 夏季上浮
    peak = ((hour >= 10) & (hour < 12)) | ((hour >= 18) & (hour < 21))
    valley = (hour >= 23) | (hour < 7)
    price = np.where(valley, 0.30, np.where(peak, 0.95, 0.60))
    price = price * np.where(season == 2, 1.06, 1.0) * (0.99 + 0.02 * rng.uniform(size=n))
    # 电网排放因子: 基准 0.581 kgCO₂/kWh, 年周期微调
    emis = 0.581 * (1 + 0.04 * np.cos(2 * np.pi * (doy - 15) / 365))
    emis = np.maximum(0.4, emis * (0.99 + 0.02 * rng.uniform(size=n)))

    rows: list[dict] = []
    for i in range(n):
        local = axis.timestamp(i) + timedelta(minutes=axis.utc_offset_minutes)
        rows.append(
            {
                TIMESTAMP_COL: local.replace(tzinfo=None),
                "e_load": float(e_load[i]),
                "h_load": float(h_load[i]),
                "c_load": float(c_load[i]),
                "t_ambient": float(t[i]),
                "ghi": float(ghi[i]),
                "electricity_price": float(price[i]),
                "grid_emission_factor": float(emis[i]),
            }
        )
    return rows


def _sample_seed() -> int:
    """样例种子: 随机不透明数(不做内容摘要派生)。

    同一项目内样例数据集按名称复用, 种子只影响首次生成; 种子进入快照,
    运行可复现性由快照保证。
    """
    return secrets.randbits(64)


def _get_or_create_sample_dataset(
    db: Session, project_id: int, region: str, resolution: str, dataset_id: int | None = None
) -> DatasetRecord:
    """查找或创建样例数据集(按项目内唯一名称; 指定 dataset_id 时直接复用)。"""
    if dataset_id is not None:
        ds = dataset_domain.get_dataset(db, dataset_id)
        if ds is not None:
            return ds
    name = f"内置样例-{region}-{resolution}"
    ds = next(
        (
            d
            for d in dataset_domain.list_datasets(db, project_id)
            if d.project_id == project_id and d.name == name
        ),
        None,
    )
    if ds is None:
        ds = _create_dataset(
            db,
            project_id,
            name,
            source_category="builtin_sample",
            license=SAMPLE_LICENSE,
            provenance={"source_category": "builtin_sample", "region": region, "resolution": resolution},
            description=f"内置合成样例数据({_sample_params(region)['name']}, {resolution})",
        )
    return ds


def _create_builtin_sample(
    db: Session,
    project_id: int,
    resolution: str,
    region: str = "shanghai",
    *,
    user_id: int | None = None,
    dataset_id: int | None = None,
) -> DatasetVersionRecord:
    """生成并保存内置样例数据版本(REQ-DATA-003, 只 flush，不提交)。

    与上传数据共用同一校验与存储路径; 记录地区/时间范围/分辨率/单位/许可证/溯源。
    每次新建写入新对象(无内容去重); 种子进入版本溯源, 运行可复现性由快照保证。

    参数:
        dataset_id: 目标数据集; 为 None 时按 "内置样例-{region}-{resolution}" 查找或创建。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    seed = _sample_seed()
    rng = np.random.default_rng(seed)
    # 数据时间轴按项目本地年: 本地 2025-01-01 00:00 起点, 即 UTC 2024-12-31 16:00
    t0_utc = datetime(2024, 12, 31, 16, tzinfo=UTC)
    axis = build_axis(resolution, utc_offset_minutes=480, t0_utc=t0_utc)
    rows = _generate_sample_rows(axis, region, rng)
    df = pd.DataFrame(rows)
    _axis, normalized, diags = validate_dataset(df, resolution, axis.utc_offset_minutes)
    if any(d.blocking for d in diags):
        # 防御: 生成器本身不应产生阻断性诊断
        raise DataValidationError(diags)
    dataset = _get_or_create_sample_dataset(db, project_id, region, resolution, dataset_id=dataset_id)
    provenance = {
        "source_category": "builtin_sample",
        "generator": "iesplan.application.datasets.lifecycle.create_builtin_sample",
        "region": region,
        "region_name": _sample_params(region)["name"],
        "resolution": resolution,
        "utc_offset_minutes": axis.utc_offset_minutes,
        "seed": seed,
        "time_range": {
            "start": axis.timestamp(0).isoformat(),
            "end": axis.timestamp(axis.n - 1).isoformat(),
        },
        "description_zh": "内置合成样例数据, 仅用于演示/教学/测试",
    }
    meta = {
        "source_category": "builtin_sample",
        "license": SAMPLE_LICENSE,
        "provenance": provenance,
        "created_reason": "builtin_sample",
    }
    return _commit_version(db, dataset, axis, normalized, diags, None, meta, actor_id=user_id)


def create_builtin_sample(
    db: Session,
    project_id: int,
    resolution: str,
    region: str = "shanghai",
    *,
    user_id: int | None = None,
    dataset_id: int | None = None,
) -> DatasetVersionRecord:
    """事务型生成内置样例数据版本；application 层统一提交或回滚。"""
    try:
        version = _create_builtin_sample(
            db, project_id, resolution, region, user_id=user_id, dataset_id=dataset_id
        )
        db.commit()
        return version
    except Exception:
        db.rollback()
        raise


__all__ = [
    "DATA_MEDIA_TYPE",
    "METADATA_MEDIA_TYPE",
    "DataValidationError",
    "add_object_ref",
    "create_builtin_sample",
    "create_dataset",
    "create_dataset_case",
    "create_sample_case",
    "default_user",
    "get_dataset",
    "get_dataset_case",
    "get_dataset_version",
    "get_dataset_version_case",
    "get_object_bytes",
    "get_template",
    "list_dataset_versions",
    "list_datasets_case",
    "list_datasets_with_latest",
    "parse_csv",
    "put_object",
    "require_project",
    "upload_dataset_version",
    "upload_dataset_version_case",
    "validate_dataset",
    "version_files_summary",
]


# ---------------------------------------------------------------------------
# HTTP 完整用例(第二轮纠偏 Wave 1 切片 3: 上传与 quota)
#
# 每个 HTTP 业务动作只转交其中一个完整用例; 用例内部按原路由顺序完成
# 授权 → 归属 → 配额 → 保存/读取, 返回与 HTTP 无关的结果(API 只做 DTO、
# 一次调用和错误/响应映射)。传输适配(封顶读取、JSON 字段解析)与输入
# DTO 校验(分辨率/偏移/白名单/大小门禁)仍在 API 层, 本层不新增校验。
# 归因口径保持原调用一致: 创建数据集沿用默认操作者, 上传版本不指定
# 操作者, 样例版本沿用调用者(与原路由传参一致)。
# ---------------------------------------------------------------------------


def _require_project_dataset(db: Session, project_id: int, dataset_id: int) -> DatasetRecord:
    """项目下数据集归属校验(口径与原路由一致: 不存在或跨项目一律 404, 含 project_id)。"""
    ds = dataset_domain.get_dataset(db, dataset_id)
    if ds is None or ds.project_id != project_id:
        raise NotFoundError(
            params={"entity_type": "dataset", "entity_id": dataset_id, "project_id": project_id}
        )
    return ds


def create_dataset_case(
    db: Session,
    user: UserRecord,
    *,
    project_id: int,
    name: str,
    source_category: str | None = None,
    license: str | None = None,
    provenance: dict | None = None,
    description: str | None = None,
) -> DatasetRecord:
    """创建数据集完整用例: edit 授权 → 创建(提交/回滚由创建步骤拥有)。"""
    ensure_access(db, user, project_id, "edit")
    return create_dataset(
        db,
        project_id,
        name,
        source_category=source_category,
        license=license,
        provenance=provenance,
        description=description,
    )


def list_datasets_case(db: Session, user: UserRecord, *, project_id: int) -> list[dict]:
    """数据集列表完整用例: view 授权 → 项目存在 → 列表+最新版本。"""
    ensure_access(db, user, project_id, "view")
    require_project(db, project_id)
    return list_datasets_with_latest(db, project_id)


def get_dataset_case(
    db: Session, user: UserRecord, *, project_id: int, dataset_id: int
) -> dict:
    """数据集详情完整用例: view 授权 → 归属 → 版本列表+文件摘要(与 HTTP 无关)。"""
    ensure_access(db, user, project_id, "view")
    dataset = _require_project_dataset(db, project_id, dataset_id)
    return {
        "dataset": dataset,
        "versions": [
            {"version": v, "files": version_files_summary(db, v.id)}
            for v in list_dataset_versions(db, dataset_id)
        ],
    }


def upload_dataset_version_case(
    db: Session,
    user: UserRecord,
    *,
    project_id: int,
    dataset_id: int,
    resolution: str,
    utc_offset_minutes: int,
    fields: dict,
    data: bytes,
    meta: dict,
) -> DatasetVersionRecord:
    """上传版本完整用例: edit 授权 → 归属 → 配额 → 校验保存(提交/回滚由保存步骤拥有)。

    异常:
        QuotaError: 配额超限(API 层转换为 413)。
        DataValidationError: 存在阻断性诊断(API 层转换为 400 信封响应)。
    """
    ensure_access(db, user, project_id, "edit")
    _require_project_dataset(db, project_id, dataset_id)
    check_upload_quota(db, user_id=user.id, project_id=project_id, incoming_bytes=len(data))
    return upload_dataset_version(
        db, dataset_id, resolution, utc_offset_minutes, fields, data, meta
    )


def get_dataset_version_case(
    db: Session,
    user: UserRecord,
    *,
    project_id: int,
    dataset_id: int,
    version_no: int,
) -> dict:
    """版本详情完整用例: view 授权 → 归属 → 版本+文件+数据引用(与 HTTP 无关)。"""
    ensure_access(db, user, project_id, "view")
    _require_project_dataset(db, project_id, dataset_id)
    return get_dataset_version(db, dataset_id, version_no)


def create_sample_case(
    db: Session,
    user: UserRecord,
    *,
    project_id: int,
    dataset_id: int,
    resolution: str,
    region: str = "shanghai",
) -> DatasetVersionRecord:
    """内置样例完整用例: edit 授权 → 归属 → 生成保存(提交/回滚由保存步骤拥有)。"""
    ensure_access(db, user, project_id, "edit")
    _require_project_dataset(db, project_id, dataset_id)
    return create_builtin_sample(
        db, project_id, resolution, region=region, user_id=user.id, dataset_id=dataset_id
    )
