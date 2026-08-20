"""数据方法:周期重复(periodic_repeat)与预测模型(prediction_model,stub 接口)。

- ``periodic_repeat``:data_repeat 核心算法——历史典型曲线按周期(day/week/year)重复
  外推扩展到任意步长(02 §2.4 period 语义);
- ``build_periodic_entry``:生成 data_repeat 设备的统一契约函数 device_entry
  (周期重复 + 容量缩放 ×params[capacity_ref]/曲线峰值,02 §6.5 build_periodic_function,
  按 05 §7.6 裁决职责归 modeling);
- ``prediction_model``:data_predict 的 stub 接口(03 §5.2)——接口与入参校验就绪,
  模型文件加载(onnx/joblib/pkl)为阶段 B,调用抛 ModelingNotImplementedError,
  禁止静默降级(05 §8.3 风险 3);
- ``build_prediction_entry``:生成 data_predict 设备的统一契约函数。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import numpy as np

from iesplan.modeling.command import DeviceRunResult
from iesplan.modeling.errors import ModelingConfigError, ModelingNotImplementedError

if TYPE_CHECKING:  # pragma: no cover
    from iesplan.modeling.devspec import DeviceSpec


def periodic_repeat(profile: dict[str, np.ndarray], n_steps: int) -> dict[str, np.ndarray]:
    """历史曲线周期重复扩展到 n_steps(03 §5.2 data_repeat)。

    参数:
        profile: 标准 csv 数据(列名 → 一维逐时数组,各列长度必须一致且 ≥ 1)。
        n_steps: 目标步数(> 0)。
    返回:
        {列名: (n_steps,) 周期外推序列};不足整周期处截断,超长处以周期回绕填充。
    """
    if n_steps < 1:
        raise ValueError(f"n_steps 必须为正,实际 {n_steps}")
    if not profile:
        raise ValueError("profile 不能为空")
    arrays = [np.asarray(v, dtype=np.float64) for v in profile.values()]
    for arr in arrays:
        if arr.ndim != 1:
            raise ValueError("profile 各列必须为一维数组")
    lengths = {arr.size for arr in arrays}
    if len(lengths) != 1:
        raise ValueError(f"profile 各列长度必须一致,实际 {sorted(lengths)}")
    period_len = lengths.pop()
    if period_len < 1:
        raise ValueError("profile 各列长度必须 ≥ 1")

    reps = -(-n_steps // period_len)  # ceil 向上取整
    return {key: np.tile(np.asarray(arr, dtype=np.float64), reps)[:n_steps] for key, arr in profile.items()}


def periodic_output_key(spec: "DeviceSpec", profile: dict[str, np.ndarray]) -> tuple[str, str]:
    """data_repeat 输出键与单位:取 time_series.outputs[0](如 electric_load 的 e_load_kw),
    未声明时退回典型曲线首列(单位 '-')。"""
    declared = spec.time_series.get("outputs")
    if declared:
        return declared[0].key, declared[0].unit
    key = next(iter(profile)) if profile else "output"
    return key, "-"


def _capacity_scale(params: dict[str, float], capacity_ref: str | None, curve: np.ndarray) -> float:
    """容量缩放系数 = params[capacity_ref] / 曲线峰值;缺 capacity_ref 或峰值为 0 → 1.0。"""
    if not capacity_ref:
        return 1.0
    peak = float(np.max(np.abs(curve))) if curve.size else 0.0
    if peak <= 0:
        return 1.0
    capacity = params.get(capacity_ref)
    if capacity is None:
        raise ModelingConfigError(f"周期重复需要容量参数 {capacity_ref!r},但 params 中缺失")
    return float(capacity) / peak


def build_periodic_entry(spec: "DeviceSpec", profile: dict[str, np.ndarray]) -> Callable:
    """生成 data_repeat 设备的统一契约函数(02 §6.5 build_periodic_function 的 modeling 版)。

    输出曲线 = 周期重复(典型曲线)× 容量缩放;输出键/单位见 ``periodic_output_key``。
    输出步长取 series 首列长度(计算快照时间轴),series 为空时取典型曲线长度。
    """
    if spec.time_series.get("inputs"):
        input_key = spec.time_series["inputs"][0].key
    else:
        input_key = next(iter(profile))
    if input_key not in profile:
        raise ModelingConfigError(f"周期重复数据缺少输入列 {input_key!r}")
    output_key, _unit = periodic_output_key(spec, profile)
    capacity_ref = spec.ports[0].capacity_ref if spec.ports else None

    def entry(
        params: dict[str, float],
        series: dict[str, np.ndarray],
        state: dict[str, float] | None,
        dt_s: float,
        prices: dict[str, float],
    ) -> DeviceRunResult:
        n_steps = len(next(iter(series.values()))) if series else len(profile[input_key])
        curve = periodic_repeat({output_key: profile[input_key]}, n_steps)[output_key]
        scale = _capacity_scale(params, capacity_ref, profile[input_key])
        return DeviceRunResult(outputs={output_key: curve * scale}, state_new=None)

    return entry


def prediction_model(model_file: str, features: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """数据-预测模型加载与预测(03 §5.2 data_predict;阶段 B 实现,此处为 stub 接口)。

    参数:
        model_file: 模型文件路径(相对设备目录,如 cop_model.onnx)。
        features: 输入特征列(列名 → 一维数组,对应 yaml function.model_file.inputs)。
    返回:
        {输出列名: (n,) 预测序列}(对应 yaml function.model_file.outputs)。
    异常:
        ModelingConfigError: model_file 缺失/features 为空/长度不一致。
        ModelingNotImplementedError: 模型加载未实现(阶段 B)。
    """
    if not model_file:
        raise ModelingConfigError("data_predict 必须声明 function.model_file")
    if not features:
        raise ModelingConfigError("prediction_model 的 features 不能为空")
    arrays = [np.asarray(v, dtype=np.float64) for v in features.values()]
    lengths = {arr.size for arr in arrays}
    if len(lengths) != 1:
        raise ValueError(f"features 各列长度必须一致,实际 {sorted(lengths)}")
    raise ModelingNotImplementedError(
        f"数据-预测模型加载未实现(阶段 B): {model_file}",
        params={"model_file": model_file},
    )


def build_prediction_entry(spec: "DeviceSpec") -> Callable:
    """生成 data_predict 设备的统一契约函数(包装 prediction_model stub,03 §5.2)。"""

    def entry(
        params: dict[str, float],
        series: dict[str, np.ndarray],
        state: dict[str, float] | None,
        dt_s: float,
        prices: dict[str, float],
    ) -> DeviceRunResult:
        # 模型输入列取自 yaml time_series.inputs(标准列);输出字段由命令 schema 声明
        outputs = prediction_model(spec.model_file, dict(series))
        return DeviceRunResult(outputs=outputs, state_new=None)

    return entry
