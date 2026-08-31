"""序列预备域服务: 校验 → 重采样/展开/预测 → 不可变计算用文件与变换回执。

处理流程(device-data-csv.md「step 与序列预备规则」):

1. 校验原始输入(ies.device-data 2.0.0, 复用 devices 规范化器: 元数据/
   设备绑定/列/单位/step/数值范围)与显式预备规格;
2. 按公共物理量语义把数据列重采样到项目基线分辨率(``resample``);
3. ``constant`` 直接展开, ``data_repeat`` 按周期确定性展开到基线全周期
   (``expand``), ``data_predict`` 用 ``ies.predict.ridge@1.0.0`` 显式
   训练输入/训练目标/预测输入完成训练与全周期预测(``ridge``);
4. 生成 ``prepared: true`` 的计算用文件, 并经同一 devices 规范化器重新
   校验闭合(单位/有效区间/点数/连续 step/基线摘要), 产出不可变
   ``PreparedSequence``(文件字节 + 摘要 + 回执 + 回执摘要 + 训练产物)。

任何校验失败返回结构化阻断诊断(``PrepOutcome.ok=False``), 不产出部分产物、
不写数据库/对象存储。本模块是纯函数(只依赖 core 与 devices 公开契约)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final

import numpy as np

from iesplan.core.contracts import ProjectBaseline
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.devices.contracts2 import DeviceModelDocument, content_sha256
from iesplan.devices.datacontract2 import (
    STEP_COL,
    ParsedDataFile2,
    canonical_table_bytes_v2,
    canonicalize_device_data_v2,
    parse_data_file_v2,
)
from iesplan.sequence_prep.contracts import (
    PREP_CANON_ALGORITHM_ID,
    PREP_CANON_ALGORITHM_VERSION,
    QUANTITY_SEMANTICS,
    RECEIPT_SCHEMA,
    RECEIPT_SCHEMA_VERSION,
    DataRepeatSpec,
    PredictFile,
    PredictSpec,
    PreparedSequence,
    PrepOutcome,
)
from iesplan.sequence_prep.expand import expand_template
from iesplan.sequence_prep.resample import resample_method, resample_series
from iesplan.sequence_prep.ridge import (
    ALGORITHM_ID,
    ALGORITHM_VERSION,
    ALPHA,
    SEED,
    STANDARDIZATION_RULE,
    artifact_bytes,
    artifact_sha256,
    predict_ridge,
    train_ridge,
)

#: 诊断码(登记于 core/diagnostics.py NEW_DIAG_CODES)
DATA_PREP_SEMANTICS_MISSING = "DATA-PREP-001"
DATA_PREP_GRID_MISALIGNED = "DATA-PREP-002"
DATA_PREP_PREDICT_INPUT_INVALID = "DATA-PREP-005"
DATA_PREP_OUTPUT_INVALID = "DATA-PREP-006"
DATA_PREP_ALREADY_PREPARED = "DATA-PREP-007"

#: 规范化 JSON 选项(回执稳定键序)
_CANONICAL_KWARGS: Final[dict[str, Any]] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


def _diag(
    code: str,
    detail: str,
    *,
    field: str | None = None,
    params: Mapping[str, object] | None = None,
) -> Diagnostic:
    """构造序列预备域阻断诊断(带字段定位)。"""
    location: dict[str, object] = {"object_type": "sequence_prep"}
    if field is not None:
        location["field"] = field
    return make_diag(
        code,
        severity="error",
        blocking=True,
        params=dict(params or {"detail": detail}),
        location=location,
    )


def _reject(diagnostics: list[Diagnostic]) -> PrepOutcome:
    return PrepOutcome(ok=False, diagnostics=diagnostics)


def _outcome(result: PreparedSequence) -> PrepOutcome:
    return PrepOutcome(ok=True, result=result)


# ---------------------------------------------------------------------------
# 预备输出收尾: 计算用文件规范化 + 输出门禁 + 变换回执
# ---------------------------------------------------------------------------


def _build_receipt(
    *,
    device_id: str,
    device_content_sha256: str,
    baseline: ProjectBaseline,
    source_mode: str,
    dataset_id: str,
    columns: list[dict[str, Any]],
    raw_sha256: str | None,
    raw_resolution: str | None,
    period: str | None,
    transformations: list[str],
    output_sha256: str,
    algorithm: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """变换回执(稳定键序 JSON)与规范化摘要(宪法 7.7)。"""
    payload = {
        "schema": RECEIPT_SCHEMA,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "canonicalizer": f"{PREP_CANON_ALGORITHM_ID}@{PREP_CANON_ALGORITHM_VERSION}",
        "source_mode": source_mode,
        "device_id": device_id,
        "device_content_sha256": device_content_sha256,
        "project_baseline_sha256": baseline.digest(),
        "resolution": baseline.resolution,
        "point_count": baseline.point_count,
        "dataset_id": dataset_id,
        "columns": columns,
        "raw_sha256": raw_sha256 or None,
        "raw_resolution": raw_resolution or None,
        "period": period or None,
        "transformations": transformations,
        "output_sha256": output_sha256,
        "algorithm": algorithm or None,
    }
    text = json.dumps(payload, **_CANONICAL_KWARGS)
    sha256 = hashlib.sha256(
        (f"{RECEIPT_SCHEMA}@{RECEIPT_SCHEMA_VERSION}\n{text}").encode()
    ).hexdigest()
    return payload, sha256


def _finalize_output(
    document: DeviceModelDocument,
    baseline: ProjectBaseline,
    *,
    source_mode: str,
    dataset_id: str,
    columns: list[str],
    units: Mapping[str, str],
    rows: list[dict[str, float]],
    data_ref: str | None,
    period: str | None,
    raw_sha256: str | None,
    raw_resolution: str | None,
    column_classification: list[dict[str, Any]],
    transformations: list[str],
    algorithm: dict[str, Any] | None = None,
    training_artifact: bytes | None = None,
    training_artifact_sha: str | None = None,
) -> tuple[PreparedSequence | None, list[Diagnostic]]:
    """生成计算用文件, 经 devices 规范化器重新校验闭合后产出不可变产物。"""
    if document.device is None:
        return None, [_diag(DATA_PREP_OUTPUT_INVALID, "设备文档缺少 device.id", field="device")]
    from iesplan.devices.datacontract2 import DeviceData2Meta

    point_count = baseline.point_count
    if any(len(row) != len(columns) for row in rows):
        return None, [_diag(DATA_PREP_OUTPUT_INVALID, "输出行字段数与列数不一致", field="steps")]
    if len(rows) != point_count:
        return None, [
            _diag(
                DATA_PREP_OUTPUT_INVALID,
                f"输出行数 {len(rows)} 与基线点数 {point_count} 不一致",
                field="steps",
                params={"actual": len(rows), "expected": point_count},
            )
        ]
    meta = DeviceData2Meta(
        schema_id="ies.device-data",
        schema_version="2.0.0",
        dataset_id=dataset_id,
        device_id=document.device.id,
        device_content_sha256=content_sha256(document),
        source_mode=source_mode,
        resolution=baseline.resolution,
        period=period,
        project_baseline_sha256=baseline.digest(),
        point_count=point_count,
        prepared=True,
        units=dict(units),
    )
    steps = list(range(point_count))
    canonical = canonical_table_bytes_v2(
        steps, (STEP_COL, *columns), rows, meta=meta
    )
    # 输出门禁: 同一规范化器重新校验(单位/有效区间/有限性/点数/连续 step/
    # 基线摘要/列绑定), 摘要链闭合
    verify = canonicalize_device_data_v2(
        canonical,
        document,
        expected_rows=point_count,
        expected_project_baseline_sha256=baseline.digest(),
        expected_data_ref=data_ref,
    )
    blocking = [d for d in verify.diagnostics if d.blocking]
    if blocking or not verify.canonical_sha256:
        return None, [
            _diag(
                DATA_PREP_OUTPUT_INVALID,
                "计算用文件未通过输出门禁(单位/有效区间/点数/连续 step/基线摘要)",
                field="steps",
            ),
            *blocking,
        ]
    receipt, receipt_sha256 = _build_receipt(
        device_id=document.device.id,
        device_content_sha256=content_sha256(document),
        baseline=baseline,
        source_mode=source_mode,
        dataset_id=dataset_id,
        columns=column_classification,
        raw_sha256=raw_sha256,
        raw_resolution=raw_resolution,
        period=period,
        transformations=transformations,
        output_sha256=verify.canonical_sha256,
        algorithm=algorithm,
    )
    return (
        PreparedSequence(
            canonical_bytes=canonical,
            canonical_sha256=verify.canonical_sha256,
            receipt=receipt,
            receipt_sha256=receipt_sha256,
            training_artifact_bytes=training_artifact,
            training_artifact_sha256=training_artifact_sha,
        ),
        [],
    )


# ---------------------------------------------------------------------------
# constant: 接口声明的值确定性展开为全周期常量序列
# ---------------------------------------------------------------------------


def prepare_constant(
    document: DeviceModelDocument, baseline: ProjectBaseline
) -> PrepOutcome:
    """把全部 ``constant`` 预定义接口展开为基线全周期计算用文件。

    常量取接口 ``source.value``(必须是数值); 展开是确定性纯函数, 相同
    文档与基线产生逐字节相同的计算用文件与回执。
    """
    if document.device is None:
        return _reject([_diag(DATA_PREP_OUTPUT_INVALID, "设备文档缺少 device.id", field="device")])
    interfaces = {
        iid: iface
        for iid, iface in document.interfaces.items()
        if iface.type == "predefined"
        and iface.source is not None
        and iface.source.mode == "constant"
    }
    if not interfaces:
        return _reject(
            [_diag(DATA_PREP_OUTPUT_INVALID, "设备文档没有 constant 预定义接口", field="interfaces")]
        )
    values: dict[str, float] = {}
    for iid, iface in interfaces.items():
        value = iface.source.value if iface.source is not None else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _reject(
                [
                    _diag(
                        DATA_PREP_OUTPUT_INVALID,
                        f"constant 接口 {iid} 的值不是数值: {value!r}",
                        field=f"interfaces.{iid}",
                    )
                ]
            )
        values[iid] = float(value)
    columns = list(interfaces)
    point_count = baseline.point_count
    rows = [{col: values[col] for col in columns} for _ in range(point_count)]
    classification = [
        {"column": iid, "semantics": None, "method": "constant_expand"} for iid in columns
    ]
    transformations = [f"constant_expand:{iid}={values[iid]}" for iid in columns]
    result, diags = _finalize_output(
        document,
        baseline,
        source_mode="constant",
        dataset_id=document.device.id,
        columns=columns,
        units={iid: interfaces[iid].unit for iid in columns},
        rows=rows,
        data_ref=None,
        period=None,
        raw_sha256=None,
        raw_resolution=None,
        column_classification=classification,
        transformations=transformations,
    )
    return _outcome(result) if result is not None else _reject(diags)


# ---------------------------------------------------------------------------
# data_repeat: 模板重采样到基线分辨率 → 按周期确定性展开到全周期
# ---------------------------------------------------------------------------


def prepare_data_repeat(
    document: DeviceModelDocument,
    baseline: ProjectBaseline,
    raw_bytes: bytes,
    data_ref: str,
    spec: DataRepeatSpec,
) -> PrepOutcome:
    """把 ``data_repeat`` 原始模板预备为基线全周期计算用文件。

    依次: 原始文件完整校验(复用 devices 规范化器) → 逐列按显式语义重采样到
    基线分辨率 → 按 ``period`` 确定性展开到 ``baseline.point_count`` 步。
    语义缺失/未知或模板行数不符返回阻断诊断, 不产出产物。
    """
    result = canonicalize_device_data_v2(raw_bytes, document, expected_data_ref=data_ref)
    blocking = [d for d in result.diagnostics if d.blocking]
    if blocking:
        return _reject(blocking)
    if result.meta.prepared:
        return _reject(
            [_diag(DATA_PREP_ALREADY_PREPARED, "data_repeat 原始文件已是预备产物", field="source_mode")]
        )
    period = result.meta.period
    source_resolution = result.meta.resolution
    if period is None:
        return _reject(
            [_diag(DATA_PREP_OUTPUT_INVALID, "data_repeat 文件缺少 period 声明", field="period")]
        )
    if result.steps != list(range(len(result.steps))):
        return _reject(
            [
                _diag(
                    DATA_PREP_GRID_MISALIGNED,
                    "原始 step 必须从 0 连续递增(模板按 step 序号确定性映射)",
                    field="steps",
                    params={"actual_start": result.steps[0] if result.steps else None},
                )
            ]
        )
    columns = list(result.column_order[1:])
    resampled: dict[str, list[float]] = {}
    classification: list[dict[str, Any]] = []
    transformations: list[str] = []
    for column in columns:
        semantics = spec.semantics.get(column)
        if semantics not in QUANTITY_SEMANTICS:
            return _reject(
                [
                    _diag(
                        DATA_PREP_SEMANTICS_MISSING,
                        f"列 {column} 未显式声明合法物理量语义",
                        field=f"semantics.{column}",
                        params={"column": column, "allowed": sorted(QUANTITY_SEMANTICS)},
                    )
                ]
            )
        values = [row.get(column) for row in result.rows]
        if any(v is None for v in values):
            return _reject(
                [_diag(DATA_PREP_OUTPUT_INVALID, f"列 {column} 含缺失值(未声明允许)", field=column)]
            )
        method, method_diags = resample_method(semantics, source_resolution, baseline.resolution)
        if method_diags:
            return _reject(method_diags)
        assert method is not None
        out, resample_diags = resample_series(
            [float(v) for v in values], source_resolution, baseline.resolution, semantics
        )
        if resample_diags:
            return _reject(resample_diags)
        assert out is not None
        resampled[column] = out
        classification.append({"column": column, "semantics": semantics, "method": method})
        transformations.append(
            f"resample:{source_resolution}->{baseline.resolution}:{semantics}:{method}"
        )
    expanded, expand_diags = expand_template(resampled, period, baseline)
    if expand_diags:
        return _reject(expand_diags)
    assert expanded is not None
    rows = [
        {column: expanded[column][i] for column in columns}
        for i in range(baseline.point_count)
    ]
    transformations.append(f"period_expand:{period}->year")
    result_out, diags = _finalize_output(
        document,
        baseline,
        source_mode="data_repeat",
        dataset_id=result.meta.dataset_id,
        columns=columns,
        units={column: result.meta.units[column] for column in columns},
        rows=rows,
        data_ref=data_ref,
        period=period,
        raw_sha256=result.raw_sha256,
        raw_resolution=source_resolution,
        column_classification=classification,
        transformations=transformations,
    )
    return _outcome(result_out) if result_out is not None else _reject(diags)


# ---------------------------------------------------------------------------
# data_predict: 显式训练输入/训练目标/预测输入 → ies.predict.ridge@1.0.0
# ---------------------------------------------------------------------------


def _parse_predict_file(
    data: bytes,
    declared_sha256: str,
    *,
    what: str,
) -> tuple[ParsedDataFile2 | None, list[Diagnostic]]:
    """结构解析 data_predict 输入文件(特征列非设备接口, 不做接口绑定)。

    校验: 文件方言/元数据/表头、step 从 0 连续递增、数值有限、声明摘要与
    实际字节摘要一致(摘要链闭合)。
    """
    parsed, diags = parse_data_file_v2(data)
    blocking = [d for d in diags if d.blocking]
    if blocking:
        return None, blocking
    if parsed is None:
        return None, diags
    if parsed.raw_sha256 != declared_sha256:
        return None, [
            _diag(
                DATA_PREP_PREDICT_INPUT_INVALID,
                f"{what} 声明摘要 {declared_sha256} 与实际字节摘要 {parsed.raw_sha256} 不一致",
                field=what,
                params={"declared_sha256": declared_sha256, "actual_sha256": parsed.raw_sha256},
            )
        ]
    if parsed.meta.prepared:
        return None, [
            _diag(
                DATA_PREP_ALREADY_PREPARED,
                f"{what} 已是预备产物, 不允许作为训练/预测输入",
                field=what,
            )
        ]
    steps: list[int] = []
    for row_no, row in enumerate(parsed.rows, start=1):
        raw_step = row[0].strip()
        if not re.fullmatch(r"0|[1-9][0-9]*", raw_step):
            return None, [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"{what} 第 {row_no} 行 step 非法: {raw_step!r}",
                    field=f"{what}.steps",
                    params={"row": row_no, "value": raw_step},
                )
            ]
        steps.append(int(raw_step))
    if steps != list(range(len(steps))):
        return None, [
            _diag(
                DATA_PREP_GRID_MISALIGNED,
                f"{what} 的 step 必须从 0 连续递增",
                field=f"{what}.steps",
            )
        ]
    return parsed, diags


def _column_values(parsed, column: str, what: str) -> tuple[list[float] | None, list[Diagnostic]]:
    """按列取有限数值(缺失/非法单元格阻断, 定位行号)。"""
    if column not in parsed.header:
        return None, [_diag(DATA_PREP_PREDICT_INPUT_INVALID, f"{what} 缺少特征列 {column}", field=column)]
    index = parsed.header.index(column)
    values: list[float] = []
    for row_no, row in enumerate(parsed.rows, start=1):
        raw = row[index].strip()
        if not raw:
            return None, [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"{what} 列 {column} 第 {row_no} 行缺失值",
                    field=column,
                    params={"row": row_no},
                )
            ]
        try:
            value = float(raw)
        except ValueError:
            return None, [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"{what} 列 {column} 第 {row_no} 行非数值: {raw!r}",
                    field=column,
                    params={"row": row_no, "value": raw},
                )
            ]
        if not np.isfinite(value):
            return None, [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"{what} 列 {column} 第 {row_no} 行非有限值",
                    field=column,
                    params={"row": row_no},
                )
            ]
        values.append(value)
    return values, []


def prepare_data_predict(
    document: DeviceModelDocument,
    baseline: ProjectBaseline,
    training_input: PredictFile,
    training_target: PredictFile,
    prediction_input: PredictFile,
    spec: PredictSpec,
) -> PrepOutcome:
    """用 ``ies.predict.ridge@1.0.0`` 完成训练与全周期预测(显式三个输入)。

    校验训练输入(特征)/训练目标(目标, 须为设备 data_predict 接口)/
    预测输入(特征, 全周期)后, 确定性训练并输出预测序列; 失败返回阻断
    诊断, 不产出训练产物或计算用文件。
    """
    feature_columns = tuple(spec.feature_columns)
    if not feature_columns:
        return _reject(
            [_diag(DATA_PREP_PREDICT_INPUT_INVALID, "feature_columns 不能为空", field="feature_columns")]
        )

    # 1) 训练输入(特征列, 非设备接口 → 结构校验)
    tin, diags = _parse_predict_file(
        training_input.data, training_input.sha256, what="training_input"
    )
    if tin is None:
        return _reject(diags)
    if tuple(tin.header[1:]) != feature_columns:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"训练输入列 {tuple(tin.header[1:])} 与声明特征列 {feature_columns} 不一致",
                    field="feature_columns",
                )
            ]
        )
    missing_units = [c for c in feature_columns if c not in tin.meta.units]
    if missing_units:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"训练输入特征列缺少 unit 声明: {missing_units}",
                    field="units",
                )
            ]
        )
    x_columns: list[list[float]] = []
    for column in feature_columns:
        values, col_diags = _column_values(tin, column, "training_input")
        if col_diags:
            return _reject(col_diags)
        assert values is not None
        x_columns.append(values)
    n_train = len(x_columns[0]) if x_columns else 0

    # 2) 训练目标(目标是设备 data_predict 接口 → 完整接口绑定校验)
    tgt = canonicalize_device_data_v2(
        training_target.data, document, expected_data_ref=spec.data_ref
    )
    tgt_blocking = [d for d in tgt.diagnostics if d.blocking]
    if tgt_blocking:
        return _reject(tgt_blocking)
    if tgt.meta.prepared:
        return _reject(
            [_diag(DATA_PREP_ALREADY_PREPARED, "训练目标文件已是预备产物", field="training_target")]
        )
    if tgt.meta.resolution != tin.meta.resolution:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"训练输入分辨率 {tin.meta.resolution} 与训练目标分辨率 {tgt.meta.resolution} 不一致",
                    field="resolution",
                )
            ]
        )
    if tgt.steps != list(range(len(tgt.steps))):
        return _reject(
            [
                _diag(
                    DATA_PREP_GRID_MISALIGNED,
                    "训练目标的 step 必须从 0 连续递增",
                    field="training_target.steps",
                )
            ]
        )
    if len(tgt.rows) != n_train:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"训练目标行数 {len(tgt.rows)} 与训练输入行数 {n_train} 不一致",
                    field="training_target",
                    params={"expected": n_train, "actual": len(tgt.rows)},
                )
            ]
        )
    target_columns = list(tgt.column_order[1:])
    overlap = set(target_columns) & set(feature_columns)
    if overlap:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"训练目标列不得同时作为特征列(避免隐式猜测): {sorted(overlap)}",
                    field="target_columns",
                )
            ]
        )
    y_columns: list[list[float]] = []
    for column in target_columns:
        values: list[float] = []
        for row in tgt.rows:
            value = row.get(column)
            if value is None:
                return _reject(
                    [_diag(DATA_PREP_PREDICT_INPUT_INVALID, f"训练目标列 {column} 含缺失值", field=column)]
                )
            values.append(float(value))
        y_columns.append(values)
    x_train = np.asarray([x_columns[i] for i in range(len(feature_columns))], dtype=np.float64).T
    y_train = np.asarray([y_columns[i] for i in range(len(target_columns))], dtype=np.float64).T
    try:
        model = train_ridge(x_train, y_train)
    except ValueError as exc:
        return _reject([_diag(DATA_PREP_PREDICT_INPUT_INVALID, str(exc), field="training")])
    training_artifact = artifact_bytes(model)
    training_artifact_sha = artifact_sha256(model)

    # 3) 预测输入(特征, 全周期): 结构校验 → 按特征语义重采样到基线分辨率
    pin, diags = _parse_predict_file(
        prediction_input.data, prediction_input.sha256, what="prediction_input"
    )
    if pin is None:
        return _reject(diags)
    if tuple(pin.header[1:]) != feature_columns:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"预测输入列 {tuple(pin.header[1:])} 与声明特征列 {feature_columns} 不一致",
                    field="feature_columns",
                )
            ]
        )
    missing_units = [c for c in feature_columns if c not in pin.meta.units]
    if missing_units:
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    f"预测输入特征列缺少 unit 声明: {missing_units}",
                    field="units",
                )
            ]
        )
    prediction_features: list[list[float]] = []
    feature_classification: list[dict[str, Any]] = []
    feature_transforms: list[str] = []
    for column in feature_columns:
        semantics = spec.feature_semantics.get(column)
        if semantics not in QUANTITY_SEMANTICS:
            return _reject(
                [
                    _diag(
                        DATA_PREP_SEMANTICS_MISSING,
                        f"特征列 {column} 未显式声明合法物理量语义",
                        field=f"feature_semantics.{column}",
                        params={"column": column, "allowed": sorted(QUANTITY_SEMANTICS)},
                    )
                ]
            )
        values, col_diags = _column_values(pin, column, "prediction_input")
        if col_diags:
            return _reject(col_diags)
        assert values is not None
        out, resample_diags = resample_series(
            values, pin.meta.resolution, baseline.resolution, semantics
        )
        if resample_diags:
            return _reject(resample_diags)
        assert out is not None
        prediction_features.append(out)
        method, _ = resample_method(semantics, pin.meta.resolution, baseline.resolution)
        assert method is not None
        feature_classification.append({"column": column, "semantics": semantics, "method": method})
        feature_transforms.append(
            f"resample:{pin.meta.resolution}->{baseline.resolution}:{semantics}:{method}"
        )
    n_predict = len(prediction_features[0]) if prediction_features else 0
    if n_predict != baseline.point_count:
        detail = (
            f"预测输入重采样后点数 {n_predict} 与基线点数 {baseline.point_count} 不一致"
            "(预测输入必须覆盖全周期)"
        )
        return _reject(
            [
                _diag(
                    DATA_PREP_PREDICT_INPUT_INVALID,
                    detail,
                    field="prediction_input",
                    params={"detail": detail, "actual": n_predict, "expected": baseline.point_count},
                )
            ]
        )
    x_pred = np.asarray(
        [prediction_features[i] for i in range(len(feature_columns))], dtype=np.float64
    ).T
    try:
        predicted = predict_ridge(model, x_pred)
    except ValueError as exc:
        return _reject([_diag(DATA_PREP_PREDICT_INPUT_INVALID, str(exc), field="prediction")])

    # 4) 输出: 目标列为设备 data_predict 接口, 预测值写入计算用文件
    rows = [
        {column: float(predicted[i, j]) for j, column in enumerate(target_columns)}
        for i in range(baseline.point_count)
    ]
    classification = [
        {"column": column, "semantics": None, "method": "ridge_predict"}
        for column in target_columns
    ]
    algorithm = {
        "id": ALGORITHM_ID,
        "version": ALGORITHM_VERSION,
        "alpha": ALPHA,
        "seed": SEED,
        "standardization": STANDARDIZATION_RULE,
        "feature_order": list(feature_columns),
        "target_order": target_columns,
        "training_input_sha256": tin.raw_sha256,
        "training_target_sha256": tgt.raw_sha256,
        "prediction_input_sha256": pin.raw_sha256,
        "training_artifact_sha256": training_artifact_sha,
        "n_train_rows": n_train,
    }
    result_out, diags = _finalize_output(
        document,
        baseline,
        source_mode="data_predict",
        dataset_id=tgt.meta.dataset_id,
        columns=target_columns,
        units={column: tgt.meta.units[column] for column in target_columns},
        rows=rows,
        data_ref=spec.data_ref,
        period=None,
        raw_sha256=training_input.sha256,
        raw_resolution=tin.meta.resolution,
        column_classification=classification,
        transformations=[f"ridge_predict:{ALGORITHM_ID}@{ALGORITHM_VERSION}", *feature_transforms],
        algorithm=algorithm,
        training_artifact=training_artifact,
        training_artifact_sha=training_artifact_sha,
    )
    return _outcome(result_out) if result_out is not None else _reject(diags)


__all__ = ["prepare_constant", "prepare_data_repeat", "prepare_data_predict"]
