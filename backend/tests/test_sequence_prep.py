"""序列预备纯领域契约/服务测试(0.6.5 事项 3)。

覆盖:
- 重采样引擎: 三类物理量语义 × 目标步长变长/变短共 6 种方法 + 同分辨率
  恒等 + 语义缺失/未知结构化阻断 + 网格无法对齐阻断;
- 周期展开: day/week/year 模板确定性展开, 普通年/闰年基线, 模板行数不符
  与展开点数不符阻断;
- ``ies.predict.ridge@1.0.0``: 带截距岭回归、特征按训练集均值/总体标准差
  标准化、零方差特征为 0、alpha=1.0 不惩罚截距、逐字节确定性、训练产物
  契约字段与摘要;
- 域服务: constant 全周期展开、data_repeat 模板重采样+展开、data_predict
  显式训练输入/训练目标/预测输入训练与预测、输出门禁摘要链闭合、全部
  失败路径返回结构化阻断诊断且不产出部分产物。

纯领域测试: 不访问数据库/对象存储/API。
"""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType

import numpy as np
import pytest

from iesplan.core.contracts import ProjectBaseline
from iesplan.devices.contracts2 import (
    DeviceInfo,
    DeviceModelDocument,
    InterfaceSpec,
    SourceSpec,
    content_sha256,
)
from iesplan.devices.datacontract2 import (
    canonicalize_device_data_v2,
    parse_data_file_v2,
)
from iesplan.sequence_prep import (
    QUANTITY_SEMANTICS,
    SEMANTICS_CUMULATIVE,
    SEMANTICS_INSTANTANEOUS,
    SEMANTICS_STATE,
    DataRepeatSpec,
    PredictFile,
    PredictSpec,
    artifact_sha256,
    expand_template,
    predict_ridge,
    prepare_constant,
    prepare_data_predict,
    prepare_data_repeat,
    resample_series,
    train_ridge,
)

BASELINE_1H = ProjectBaseline(resolution="1h", leap_year=False)
BASELINE_1H_LEAP = ProjectBaseline(resolution="1h", leap_year=True)


def _iface(
    iid: str,
    mode: str,
    *,
    unit: str = "kW",
    data_ref: str | None = None,
    value: float | None = None,
    minimum: float | None = 0.0,
    maximum: float | None = None,
) -> InterfaceSpec:
    return InterfaceSpec(
        id=iid,
        type="predefined",
        carrier="electric",
        unit=unit,
        valid_range=(minimum, maximum),
        source=SourceSpec(mode=mode, value=value, data_ref=data_ref),
    )


def make_document(
    *,
    device_id: str = "acme.device.test_1",
    interfaces: dict[str, InterfaceSpec] | None = None,
) -> DeviceModelDocument:
    return DeviceModelDocument(
        device=DeviceInfo(id=device_id, names={"zh-CN": "测试设备"}),
        interfaces=MappingProxyType(interfaces or {}),
    )


def data_csv(
    *,
    dataset_id: str,
    document: DeviceModelDocument,
    source_mode: str,
    resolution: str,
    columns: list[str],
    rows: list[list[float]],
    units: dict[str, str] | None = None,
    period: str | None = None,
    prepared: bool = False,
    baseline_sha: str | None = None,
    point_count: int | None = None,
) -> bytes:
    """构造 ies.device-data 2.0.0 CSV 字节(与 datacontract2 元数据契约一致)。"""
    assert document.device is not None
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.0.0",
        f"# dataset_id: {dataset_id}",
        f"# device_id: {document.device.id}",
        f"# device_content_sha256: {content_sha256(document)}",
        f"# source_mode: {source_mode}",
        f"# resolution: {resolution}",
    ]
    if period is not None:
        lines.append(f"# period: {period}")
    if prepared:
        lines.extend([
            f"# project_baseline_sha256: {baseline_sha}",
            f"# point_count: {point_count}",
            "# prepared: true",
        ])
    for column in columns:
        lines.append(f"# unit.{column}: {(units or {}).get(column, 'kW')}")
    lines.append("step," + ",".join(columns))
    for step, row in enumerate(rows):
        lines.append(",".join([str(step), *(str(v) for v in row)]))
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 重采样引擎
# ---------------------------------------------------------------------------


class TestResample:
    def test_instantaneous_coarser_time_weighted_mean(self):
        values, diags = resample_series([1, 2, 3, 4], "15min", "1h", SEMANTICS_INSTANTANEOUS)
        assert not diags
        assert values == [2.5]

    def test_instantaneous_finer_linear_interpolation(self):
        values, diags = resample_series([0.0, 10.0], "1h", "15min", SEMANTICS_INSTANTANEOUS)
        assert not diags
        # 0, 2.5, 5, 7.5, 10, 10, 10, 10(末区间后按末值前向保持)
        assert values == [0.0, 2.5, 5.0, 7.5, 10.0, 10.0, 10.0, 10.0]

    def test_cumulative_coarser_sum(self):
        values, diags = resample_series([1, 2, 3, 4], "15min", "1h", SEMANTICS_CUMULATIVE)
        assert not diags
        assert values == [10.0]  # 求和并保持区间总量

    def test_cumulative_finer_pro_rata(self):
        values, diags = resample_series([100.0], "1h", "15min", SEMANTICS_CUMULATIVE)
        assert not diags
        assert values == [25.0, 25.0, 25.0, 25.0]  # 按时长比例分配, 总量不变

    def test_state_coarser_interval_last(self):
        values, diags = resample_series([1, 2, 3, 4], "15min", "1h", SEMANTICS_STATE)
        assert not diags
        assert values == [4.0]  # 目标区间内最后一个观测值

    def test_state_finer_forward_hold(self):
        values, diags = resample_series([7.0], "1h", "15min", SEMANTICS_STATE)
        assert not diags
        assert values == [7.0, 7.0, 7.0, 7.0]

    def test_identity_resolution_noop(self):
        values, diags = resample_series([1.5, 2.5], "1h", "1h", SEMANTICS_INSTANTANEOUS)
        assert not diags
        assert values == [1.5, 2.5]

    def test_unknown_semantics_blocked(self):
        values, diags = resample_series([1.0], "1h", "15min", "average")
        assert values is None
        assert len(diags) == 1
        assert diags[0].code == "DATA-PREP-001"
        assert diags[0].blocking

    def test_grid_misaligned_blocked(self):
        values, diags = resample_series([1.0, 2.0, 3.0], "15min", "1h", SEMANTICS_INSTANTANEOUS)
        assert values is None
        assert diags[0].code == "DATA-PREP-002"
        assert diags[0].blocking


# ---------------------------------------------------------------------------
# 周期展开
# ---------------------------------------------------------------------------


class TestExpandTemplate:
    def test_day_template_normal_year(self):
        template = {"power": [float(i) for i in range(24)]}
        out, diags = expand_template(template, "day", BASELINE_1H)
        assert not diags
        assert len(out["power"]) == 8760
        assert out["power"][:24] == template["power"]
        assert out["power"][24:48] == template["power"]

    def test_week_template_normal_year(self):
        template = {"power": [float(i % 24) for i in range(24 * 7)]}
        out, diags = expand_template(template, "week", BASELINE_1H)
        assert not diags
        assert len(out["power"]) == 8760  # 52 周 + 1 天
        assert out["power"][: 24 * 7] == template["power"]
        assert out["power"][24 * 7 * 52:] == template["power"][:24]  # 余下 1 天

    def test_year_template_leap_year_appends_first_day(self):
        template = {"power": [float(i % 24) for i in range(24 * 365)]}
        out, diags = expand_template(template, "year", BASELINE_1H_LEAP)
        assert not diags
        assert len(out["power"]) == 8784  # 366 天
        assert out["power"][24 * 365:] == template["power"][:24]

    def test_template_row_count_mismatch_blocked(self):
        template = {"power": [1.0, 2.0]}  # 期望 24 行
        out, diags = expand_template(template, "day", BASELINE_1H)
        assert out is None
        assert diags[0].code == "DATA-PREP-003"
        assert diags[0].blocking


def _ridge_closed_form(n: float, x: float, alpha: float = 1.0) -> float:
    """单特征 + 线性目标 y=2x+1 的岭回归闭式解(独立推导, 交叉验证实现)。

    特征按训练集均值/总体标准差标准化, alpha 不惩罚截距:
    w = 2nσ/(n+α), 截距 = 训练目标均值 2μ+1; ŷ(x) = 2n(x-μ)/(n+α) + 2μ+1。
    """

    mu = (n - 1) / 2.0
    return 2.0 * n * (x - mu) / (n + alpha) + 2.0 * mu + 1.0


# ---------------------------------------------------------------------------
# ies.predict.ridge@1.0.0
# ---------------------------------------------------------------------------


class TestRidge:
    def test_recovers_linear_relation_with_unpenalized_intercept(self):
        x = np.arange(20.0).reshape(-1, 1)
        y = (2.0 * x + 1.0).reshape(-1, 1)
        model = train_ridge(x, y)
        # 特征按训练集标准化(零均值): 不惩罚截距 ⇒ 截距 = 训练目标均值
        assert model.intercepts["0"] == pytest.approx(20.0, abs=1e-6)
        assert model.means["0"] == pytest.approx(9.5, abs=1e-6)
        assert model.stds["0"] == pytest.approx(np.std(x), abs=1e-9)
        # 系数在标准化空间: w = 2nσ/(n+α)(岭收缩, alpha=1.0 不惩罚截距)
        assert model.coefficients["0"]["0"] == pytest.approx(
            2.0 * 20.0 * np.std(x) / 21.0, abs=1e-6
        )
        # 预测与独立推导的闭式解一致
        for test_x, expected in ((5.0, _ridge_closed_form(20.0, 5.0)),
                                 (100.0, _ridge_closed_form(20.0, 100.0))):
            assert predict_ridge(model, np.array([[test_x]]))[0, 0] == pytest.approx(
                expected, abs=1e-6
            )

    def test_regularized_recovery_with_large_sample(self):
        # alpha=1.0 正则: 训练均值处无收缩误差, 近均值点误差 = 2α(x-μ)/(n+α)
        n = 500.0
        x = np.arange(n).reshape(-1, 1)
        y = (2.0 * x + 1.0).reshape(-1, 1)
        model = train_ridge(x, y)
        mu = (n - 1) / 2.0
        # 训练均值处: 截距(不惩罚)精确恢复
        assert predict_ridge(model, np.array([[mu]]))[0, 0] == pytest.approx(2 * mu + 1, abs=1e-6)
        # 近均值点: 与闭式解一致(误差显式可算)
        assert predict_ridge(model, np.array([[mu + 100.0]]))[0, 0] == pytest.approx(
            _ridge_closed_form(n, mu + 100.0), abs=1e-6
        )

    def test_zero_variance_feature_maps_to_zero(self):
        x = np.column_stack([np.arange(10.0), np.full(10, 5.0)])
        y = (3.0 * x[:, 0] + 7.0).reshape(-1, 1)
        model = train_ridge(x, y)
        assert model.zero_variance["1"] is True
        assert model.stds["1"] == 0.0
        # 零方差特征标准化为 0: 预测结果对该特征取值不敏感
        assert predict_ridge(model, np.array([[1.0, 999.0]])) == pytest.approx(
            predict_ridge(model, np.array([[1.0, -999.0]])), abs=1e-9
        )

    def test_multiple_targets_independent(self):
        n = 500.0
        x = np.arange(n).reshape(-1, 1)
        y = np.column_stack([2.0 * x[:, 0], -x[:, 0] + 5.0])
        model = train_ridge(x, y)
        # 每个目标独立训练: 闭式解 y1 = 2x(均值 2μ), y2 = -x+5(均值 5-μ)
        mu = (n - 1) / 2.0
        pred = predict_ridge(model, np.array([[3.0]]))
        y1_expected = 2.0 * n * (3.0 - mu) / (n + 1.0) + 2.0 * mu
        y2_expected = -1.0 * n * (3.0 - mu) / (n + 1.0) + (5.0 - mu)
        assert pred[0, 0] == pytest.approx(y1_expected, abs=1e-6)
        assert pred[0, 1] == pytest.approx(y2_expected, abs=1e-6)

    def test_artifact_is_byte_deterministic(self):
        x = np.arange(20.0).reshape(-1, 1)
        y = (2.0 * x + 1.0).reshape(-1, 1)
        sha1 = artifact_sha256(train_ridge(x, y))
        sha2 = artifact_sha256(train_ridge(x, y))
        assert sha1 == sha2
        from iesplan.sequence_prep.ridge import artifact_bytes

        model = train_ridge(x, y)
        assert artifact_bytes(model) == artifact_bytes(train_ridge(x, y))

    def test_artifact_contract_fields(self):
        from iesplan.sequence_prep.ridge import (
            ALGORITHM_ID,
            ALGORITHM_VERSION,
            ALPHA,
            SEED,
            artifact_bytes,
        )

        x = np.arange(10.0).reshape(-1, 1)
        y = (2.0 * x).reshape(-1, 1)
        payload = json.loads(artifact_bytes(train_ridge(x, y)).decode("utf-8"))
        assert payload["schema"] == "ies.predict.artifact"
        assert payload["algorithm_id"] == ALGORITHM_ID == "ies.predict.ridge"
        assert payload["algorithm_version"] == ALGORITHM_VERSION == "1.0.0"
        assert payload["alpha"] == ALPHA == 1.0
        assert payload["seed"] == SEED == 42
        assert payload["standardization"] == "train_mean_population_std_zero_variance_zero"
        assert payload["feature_order"] == ["0"]
        assert payload["target_order"] == ["0"]
        assert "features" in payload and "coefficients" in payload and "intercepts" in payload

    def test_training_input_validation(self):
        with pytest.raises(ValueError):
            train_ridge(np.zeros((0, 2)), np.zeros((0, 1)))
        with pytest.raises(ValueError):
            train_ridge(np.zeros((3, 2)), np.zeros((4, 1)))
        with pytest.raises(ValueError):
            train_ridge(np.array([[np.nan, 1.0]]), np.zeros((1, 1)))


# ---------------------------------------------------------------------------
# 域服务: constant
# ---------------------------------------------------------------------------


class TestPrepareConstant:
    def test_expands_full_period_constant(self):
        document = make_document(interfaces={
            "power": _iface("power", "constant", value=5.0),
        })
        outcome = prepare_constant(document, BASELINE_1H)
        assert outcome.ok and outcome.result is not None
        seq = outcome.result
        assert len(seq.canonical_bytes) > 0
        # 输出门禁: 同一规范化器重新校验无阻断
        verify = canonicalize_device_data_v2(
            seq.canonical_bytes, document,
            expected_rows=8760,
            expected_project_baseline_sha256=BASELINE_1H.digest(),
        )
        assert not [d for d in verify.diagnostics if d.blocking]
        assert verify.meta.prepared
        assert verify.meta.point_count == 8760
        assert verify.meta.source_mode == "constant"
        assert verify.meta.project_baseline_sha256 == BASELINE_1H.digest()
        assert all(row["power"] == 5.0 for row in verify.rows)
        assert seq.receipt["source_mode"] == "constant"
        assert seq.receipt["transformations"] == ["constant_expand:power=5.0"]
        assert seq.receipt_sha256

    def test_non_numeric_constant_blocked(self):
        document = DeviceModelDocument(
            device=DeviceInfo(id="acme.device.test_1"),
            interfaces=MappingProxyType({
                "flag": InterfaceSpec(
                    id="flag", type="predefined", carrier="electric", unit="kW",
                    valid_range=(0.0, None),
                    source=SourceSpec(mode="constant", value=True),
                )
            }),
        )
        outcome = prepare_constant(document, BASELINE_1H)
        assert not outcome.ok
        assert outcome.result is None
        assert outcome.diagnostics[0].blocking


# ---------------------------------------------------------------------------
# 域服务: data_repeat
# ---------------------------------------------------------------------------


class TestPrepareDataRepeat:
    def _document(self) -> DeviceModelDocument:
        return make_document(interfaces={
            "electric_demand": _iface("electric_demand", "data_repeat", data_ref="load_data",
                                      minimum=0.0, maximum=1000.0),
        })

    def test_resample_and_expand_full_period(self):
        document = self._document()
        raw = data_csv(
            dataset_id="test.load.profile", document=document, source_mode="data_repeat",
            resolution="15min", columns=["electric_demand"],
            rows=[[float(10 + step)] for step in range(96)],  # 一天 15min 模板
            period="day",
        )
        outcome = prepare_data_repeat(
            document, BASELINE_1H, raw, "load_data",
            DataRepeatSpec(semantics={"electric_demand": SEMANTICS_INSTANTANEOUS}),
        )
        assert outcome.ok and outcome.result is not None
        seq = outcome.result
        verify = canonicalize_device_data_v2(
            seq.canonical_bytes, document,
            expected_rows=8760,
            expected_project_baseline_sha256=BASELINE_1H.digest(),
            expected_data_ref="load_data",
        )
        assert not [d for d in verify.diagnostics if d.blocking]
        assert verify.meta.prepared and verify.meta.point_count == 8760
        # 第一个目标小时的均值 = 前 4 个 15min 值的时间加权平均
        assert verify.rows[0]["electric_demand"] == pytest.approx(sum(range(10, 14)) / 4)
        # 逐小时模板周期重复: 第 2 天第 1 小时与第 1 天第 1 小时相同
        assert verify.rows[24]["electric_demand"] == verify.rows[0]["electric_demand"]
        assert seq.receipt["period"] == "day"
        assert "resample:15min->1h:instantaneous:time_weighted_mean" in seq.receipt["transformations"]
        assert "period_expand:day->year" in seq.receipt["transformations"]

    def test_missing_semantics_blocked(self):
        document = self._document()
        raw = data_csv(
            dataset_id="test.load.profile", document=document, source_mode="data_repeat",
            resolution="1h", columns=["electric_demand"],
            rows=[[float(step)] for step in range(24)], period="day",
        )
        outcome = prepare_data_repeat(
            document, BASELINE_1H, raw, "load_data", DataRepeatSpec(semantics={})
        )
        assert not outcome.ok and outcome.result is None
        assert outcome.diagnostics[0].code == "DATA-PREP-001"
        assert outcome.diagnostics[0].blocking

    def test_already_prepared_input_blocked(self):
        document = self._document()
        # 完整且合法的预备产物(8760 行, 已绑定基线)才到达"禁止重复预备"检查
        raw = data_csv(
            dataset_id="test.load.profile", document=document, source_mode="data_repeat",
            resolution="1h", columns=["electric_demand"],
            rows=[[float(step % 24)] for step in range(8760)], period="day",
            prepared=True, baseline_sha=BASELINE_1H.digest(), point_count=8760,
        )
        outcome = prepare_data_repeat(
            document, BASELINE_1H, raw, "load_data",
            DataRepeatSpec(semantics={"electric_demand": SEMANTICS_INSTANTANEOUS}),
        )
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-007"

    def test_non_contiguous_steps_blocked(self):
        document = self._document()
        assert document.device is not None
        lines = [
            "# schema: ies.device-data",
            "# schema_version: 2.0.0",
            "# dataset_id: test.load.profile",
            f"# device_id: {document.device.id}",
            f"# device_content_sha256: {content_sha256(document)}",
            "# source_mode: data_repeat",
            "# resolution: 1h",
            "# period: day",
            "# unit.electric_demand: kW",
            "step,electric_demand",
            *[f"{step},{10 + step}" for step in range(23)],
            "24,33",  # 行数满足周期但 step 跳步(0..22,24)
        ]
        raw = ("\n".join(lines) + "\n").encode()
        outcome = prepare_data_repeat(
            document, BASELINE_1H, raw, "load_data",
            DataRepeatSpec(semantics={"electric_demand": SEMANTICS_INSTANTANEOUS}),
        )
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-002"


# ---------------------------------------------------------------------------
# 域服务: data_predict
# ---------------------------------------------------------------------------


def _predict_document() -> DeviceModelDocument:
    return make_document(interfaces={
        "power": _iface("power", "data_predict", data_ref="predict_ref",
                        minimum=0.0, maximum=1000.0),
    })


def _predict_spec() -> PredictSpec:
    return PredictSpec(
        data_ref="predict_ref",
        training_input_ref="train_in",
        training_target_ref="train_target",
        prediction_input_ref="pred_in",
        feature_columns=("temp", "ghi"),
        feature_semantics={"temp": SEMANTICS_INSTANTANEOUS, "ghi": SEMANTICS_INSTANTANEOUS},
    )


def _predict_files(document: DeviceModelDocument, *, n_train: int = 24, n_predict: int = 24):
    rng = np.random.default_rng(7)
    temp = rng.normal(20.0, 5.0, n_train)
    ghi = np.clip(rng.normal(500.0, 200.0, n_train), 0, None)
    power = 2.0 * temp + 0.5 * ghi / 100.0 + 10.0
    train_in = data_csv(
        dataset_id="train.in", document=document, source_mode="data_predict",
        resolution="1h", columns=["temp", "ghi"], rows=[[t, g] for t, g in zip(temp, ghi, strict=True)],
        units={"temp": "°C", "ghi": "W/m²"},
    )
    train_target = data_csv(
        dataset_id="train.target", document=document, source_mode="data_predict",
        resolution="1h", columns=["power"], rows=[[v] for v in power],
    )
    t_pred = np.linspace(10.0, 30.0, n_predict)
    g_pred = np.linspace(100.0, 900.0, n_predict)
    pred_in = data_csv(
        dataset_id="pred.in", document=document, source_mode="data_predict",
        resolution="1h", columns=["temp", "ghi"],
        rows=[[t, g] for t, g in zip(t_pred, g_pred, strict=True)],
        units={"temp": "°C", "ghi": "W/m²"},
    )
    return (
        PredictFile(data=train_in, sha256=hashlib.sha256(train_in).hexdigest()),
        PredictFile(data=train_target, sha256=hashlib.sha256(train_target).hexdigest()),
        PredictFile(data=pred_in, sha256=hashlib.sha256(pred_in).hexdigest()),
    )


class TestPrepareDataPredict:
    def test_train_and_predict_full_period(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        outcome = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, _predict_spec())
        assert outcome.ok and outcome.result is not None
        seq = outcome.result
        verify = canonicalize_device_data_v2(
            seq.canonical_bytes, document,
            expected_rows=8760,
            expected_project_baseline_sha256=BASELINE_1H.digest(),
            expected_data_ref="predict_ref",
        )
        assert not [d for d in verify.diagnostics if d.blocking]
        assert verify.meta.point_count == 8760
        assert verify.column_order == ("step", "power")
        assert all(row["power"] is not None for row in verify.rows)
        # 回执固定算法契约
        algorithm = seq.receipt["algorithm"]
        assert algorithm["id"] == "ies.predict.ridge"
        assert algorithm["version"] == "1.0.0"
        assert algorithm["alpha"] == 1.0
        assert algorithm["seed"] == 42
        assert algorithm["feature_order"] == ["temp", "ghi"]
        assert algorithm["target_order"] == ["power"]
        assert algorithm["training_input_sha256"] == tin.sha256
        assert algorithm["training_target_sha256"] == tgt.sha256
        assert algorithm["prediction_input_sha256"] == pin.sha256
        assert algorithm["training_artifact_sha256"] == seq.training_artifact_sha256
        assert seq.training_artifact_bytes is not None

    def test_deterministic_output(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        out1 = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, _predict_spec())
        out2 = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, _predict_spec())
        assert out1.ok and out2.ok
        assert out1.result.canonical_sha256 == out2.result.canonical_sha256
        assert out1.result.receipt_sha256 == out2.result.receipt_sha256
        assert out1.result.training_artifact_sha256 == out2.result.training_artifact_sha256
        assert out1.result.canonical_bytes == out2.result.canonical_bytes

    def test_row_count_mismatch_blocked(self):
        document = _predict_document()
        _, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        rows = [[20.0, 500.0]] * 10
        tin2 = data_csv(
            dataset_id="train.in", document=document, source_mode="data_predict",
            resolution="1h", columns=["temp", "ghi"], rows=rows,
            units={"temp": "°C", "ghi": "W/m²"},
        )
        outcome = prepare_data_predict(
            document, BASELINE_1H,
            PredictFile(data=tin2, sha256=hashlib.sha256(tin2).hexdigest()),
            tgt, pin, _predict_spec(),
        )
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-005"

    def test_prediction_coverage_mismatch_blocked(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=10)
        outcome = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, _predict_spec())
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-005"
        assert "全周期" in outcome.diagnostics[0].params["detail"]

    def test_target_overlap_with_features_blocked(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        spec = PredictSpec(
            data_ref="predict_ref",
            training_input_ref="train_in",
            training_target_ref="train_target",
            prediction_input_ref="pred_in",
            feature_columns=("temp", "ghi", "power"),  # power 同时是目标列
            feature_semantics={"temp": SEMANTICS_INSTANTANEOUS, "ghi": SEMANTICS_INSTANTANEOUS,
                               "power": SEMANTICS_INSTANTANEOUS},
        )
        outcome = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, spec)
        assert not outcome.ok
        assert any(d.code == "DATA-PREP-005" for d in outcome.diagnostics)

    def test_missing_feature_semantics_blocked(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        spec = PredictSpec(
            data_ref="predict_ref",
            training_input_ref="train_in",
            training_target_ref="train_target",
            prediction_input_ref="pred_in",
            feature_columns=("temp", "ghi"),
            feature_semantics={"temp": SEMANTICS_INSTANTANEOUS},  # ghi 缺失
        )
        outcome = prepare_data_predict(document, BASELINE_1H, tin, tgt, pin, spec)
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-001"

    def test_sha256_mismatch_blocked(self):
        document = _predict_document()
        tin, tgt, pin = _predict_files(document, n_train=24, n_predict=8760)
        bad = PredictFile(data=tin.data, sha256="0" * 64)
        outcome = prepare_data_predict(document, BASELINE_1H, bad, tgt, pin, _predict_spec())
        assert not outcome.ok
        assert outcome.diagnostics[0].code == "DATA-PREP-005"

    def test_resample_prediction_input_to_baseline(self):
        # 预测输入 15min(全周期 35040 步) → 基线 1h(8760 步)
        document = _predict_document()
        rng = np.random.default_rng(11)
        n = 35040
        temp = rng.normal(20.0, 5.0, n)
        ghi = np.clip(rng.normal(500.0, 200.0, n), 0, None)
        tin = data_csv(
            dataset_id="train.in", document=document, source_mode="data_predict",
            resolution="15min", columns=["temp", "ghi"],
            rows=[[t, g] for t, g in zip(temp[:96], ghi[:96], strict=True)],
            units={"temp": "°C", "ghi": "W/m²"},
        )
        power = 2.0 * temp[:96] + 0.5 * ghi[:96] / 100.0 + 10.0
        tgt = data_csv(
            dataset_id="train.target", document=document, source_mode="data_predict",
            resolution="15min", columns=["power"], rows=[[v] for v in power],
        )
        pin = data_csv(
            dataset_id="pred.in", document=document, source_mode="data_predict",
            resolution="15min", columns=["temp", "ghi"],
            rows=[[t, g] for t, g in zip(temp, ghi, strict=True)],
            units={"temp": "°C", "ghi": "W/m²"},
        )
        outcome = prepare_data_predict(
            document, BASELINE_1H,
            PredictFile(data=tin, sha256=hashlib.sha256(tin).hexdigest()),
            PredictFile(data=tgt, sha256=hashlib.sha256(tgt).hexdigest()),
            PredictFile(data=pin, sha256=hashlib.sha256(pin).hexdigest()),
            _predict_spec(),
        )
        assert outcome.ok and outcome.result is not None
        assert outcome.result.receipt["point_count"] == 8760
        assert any(
            "resample:15min->1h:instantaneous" in t
            for t in outcome.result.receipt["transformations"]
        )


# ---------------------------------------------------------------------------
# 语义分类与摘要链
# ---------------------------------------------------------------------------


class TestContracts:
    def test_quantity_semantics_fixed_three(self):
        assert QUANTITY_SEMANTICS == ("instantaneous", "cumulative", "state")

    def test_receipt_sha256_chain(self):
        document = make_document(interfaces={
            "power": _iface("power", "constant", value=1.0),
        })
        outcome = prepare_constant(document, BASELINE_1H)
        assert outcome.ok and outcome.result is not None
        seq = outcome.result
        recomputed = hashlib.sha256(
            (
                "ies.sequence-prep.receipt@1.0.0\n"
                + json.dumps(
                    dict(seq.receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            ).encode("utf-8")
        ).hexdigest()
        assert seq.receipt_sha256 == recomputed
        assert seq.canonical_sha256 == hashlib.sha256(seq.canonical_bytes).hexdigest()

    def test_prepared_file_roundtrip_parse(self):
        document = make_document(interfaces={
            "power": _iface("power", "constant", value=2.5),
        })
        outcome = prepare_constant(document, BASELINE_1H)
        assert outcome.ok and outcome.result is not None
        parsed, diags = parse_data_file_v2(outcome.result.canonical_bytes)
        assert parsed is not None and not [d for d in diags if d.blocking]
        assert parsed.meta.prepared
        assert parsed.meta.point_count == 8760
