"""建模模块测试(纯 pytest,无需数据库)。

覆盖:命令 id 约定 / 设备规格校验 / 机理基础函数(简单传热、功率平衡、PV、锅炉、
冷机、燃气、热泵 COP)/ 机理命令生成与输入输出 schema(字段+单位)/ 统一调用契约
call_command / 周期重复(periodic repeat 时间序列)/ 预测模型 stub 接口 /
有状态模型的状态输入输出传递与钳制 / 非法输入拒载。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from iesplan.core.contracts.parameters import ParameterSpec
from iesplan.core.errors import AppError, NotFoundError
from iesplan.modeling import (
    MECHANISM_FUNCTIONS,
    MODEL_METHOD_DATA_PREDICT,
    MODEL_METHOD_DATA_REPEAT,
    MODEL_METHOD_MECHANISM,
    DeviceRunResult,
    DeviceSpec,
    ModelingConfigError,
    ModelingNotImplementedError,
    ModuleCommand,
    PortSpec,
    SeriesSpec,
    StateSpec,
    boiler_output,
    build_command,
    call_command,
    chiller_output,
    clear_commands,
    gas_volume_m3,
    get_command,
    heat_pump_cop,
    heat_transfer_q,
    list_commands,
    make_command_id,
    parse_command_id,
    periodic_repeat,
    power_balance,
    prediction_model,
    pv_output,
    register_command,
    replace_all_commands,
    resolve_function_ref,
    simulate_battery,
    snapshot,
    validate_spec,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """用例隔离:每例清空命令注册表。"""
    clear_commands()
    yield
    clear_commands()


def _p(name: str, unit: str, **kw) -> ParameterSpec:
    return ParameterSpec(name=name, unit=unit, **kw)


# ---------------------------------------------------------------------------
# 测试夹具:三类设备规格(机理无状态 pv / 机理有状态 battery / 数据周期重复 load)
# ---------------------------------------------------------------------------


def make_pv_spec() -> DeviceSpec:
    return DeviceSpec(
        type_id="ies.device.pv",
        version="1.4.0",
        name_zh="光伏",
        name_en="PV",
        energy_carriers=["solar", "electric"],
        is_load=False,
        capabilities=["pv", "controllable"],
        parameters={
            "rated_capacity_kwp": _p("rated_capacity_kwp", "kWp", min=0, max=1e6, default=0),
            "efficiency": _p("efficiency", "-", min=0.05, max=0.5, default=0.20),
        },
        model_method=MODEL_METHOD_MECHANISM,
        stateful=False,
        model_function="iesplan.modeling.functions.pv_output",
        ports=(
            PortSpec(
                name="pv_out",
                port_type="electric",
                direction="out",
                energy_carrier="electric",
                capacity_ref="rated_capacity_kwp",
            ),
        ),
        time_series={
            "inputs": (SeriesSpec(key="ghi", unit="W/m²"), SeriesSpec(key="t_ambient", unit="°C")),
            "outputs": (),
        },
    )


def make_battery_spec() -> DeviceSpec:
    return DeviceSpec(
        type_id="ies.device.battery",
        version="1.5.0",
        name_zh="电池储能",
        name_en="Battery",
        energy_carriers=["electric"],
        is_load=False,
        capabilities=["storage", "controllable"],
        parameters={
            "capacity_kwh": _p("capacity_kwh", "kWh", min=0, max=1e7, default=0),
            "initial_soc": _p("initial_soc", "-", min=0, max=1.0, default=0.5),
            "charge_efficiency": _p("charge_efficiency", "-", min=0.5, max=1.0, default=0.95),
            "discharge_efficiency": _p("discharge_efficiency", "-", min=0.5, max=1.0, default=0.95),
            "min_soc": _p("min_soc", "-", min=0, max=0.5, default=0.1),
            "max_soc": _p("max_soc", "-", min=0.5, max=1.0, default=0.9),
        },
        model_method=MODEL_METHOD_MECHANISM,
        stateful=True,
        model_function="iesplan.modeling.functions.simulate_battery",
        ports=(
            PortSpec(name="bat_in", port_type="electric", direction="in", energy_carrier="electric"),
            PortSpec(name="bat_out", port_type="electric", direction="out", energy_carrier="electric"),
        ),
        states=(StateSpec(key="soc", unit="-", initial_ref="initial_soc",
                          bounds={"min_ref": "min_soc", "max_ref": "max_soc"}),),
        time_series={"inputs": (), "outputs": ()},
    )


def make_load_spec() -> DeviceSpec:
    return DeviceSpec(
        type_id="ies.device.electric_load",
        version="1.2.0",
        name_zh="电负荷",
        name_en="Electric Load",
        energy_carriers=["electric"],
        is_load=True,
        capabilities=["load"],
        parameters={
            "peak_power_kw": _p("peak_power_kw", "kW", min=0, max=1e7, default=0),
        },
        model_method=MODEL_METHOD_DATA_REPEAT,
        stateful=False,
        data_file="electric_load.csv",
        ports=(PortSpec(name="load_in", port_type="electric", direction="in",
                        energy_carrier="electric", capacity_ref="peak_power_kw"),),
        time_series={
            "inputs": (SeriesSpec(key="e_load", unit="kWh", required=True, period="day"),),
            "outputs": (SeriesSpec(key="e_load_kw", unit="kW"),),
        },
    )


def make_hp_forecast_spec() -> DeviceSpec:
    return DeviceSpec(
        type_id="ies.device.heat_pump_dr",
        version="1.0.0",
        name_zh="热泵(数据预测)",
        name_en="Heat Pump (forecast)",
        energy_carriers=["electric", "heat", "cool"],
        is_load=False,
        capabilities=["heat_pump", "controllable"],
        parameters={
            "rated_heat_kw": _p("rated_heat_kw", "kW", min=0, max=1e6, default=0),
        },
        model_method=MODEL_METHOD_DATA_PREDICT,
        stateful=True,
        model_file="cop_model.onnx",
        states=(StateSpec(key="cop_est", unit="-", initial_ref="cop_init"),),
        time_series={
            "inputs": (
                SeriesSpec(key="t_ambient", unit="°C", required=True),
                SeriesSpec(key="h_load", unit="kWh", required=True),
            ),
            "outputs": (SeriesSpec(key="cop", unit="-"),),
        },
    )


# ---------------------------------------------------------------------------
# 命令 id 约定(03 §5.2 make_command_id)
# ---------------------------------------------------------------------------


def test_make_and_parse_command_id():
    cid = make_command_id("ies.device.pv", "mechanism", "1.4.0")
    assert cid == "ies.command.model.ies.device.pv.mechanism.1.4.0"
    assert parse_command_id(cid) == ("ies.device.pv", "mechanism", "1.4.0")


def test_parse_command_id_invalid():
    with pytest.raises(ValueError):
        parse_command_id("ies.device.pv.mechanism")
    with pytest.raises(ValueError):
        parse_command_id("garbage")


# ---------------------------------------------------------------------------
# 设备规格校验(devspec.validate_spec,02 §3 约束)
# ---------------------------------------------------------------------------


def test_validate_spec_ok():
    assert validate_spec(make_pv_spec()) == []
    assert validate_spec(make_battery_spec()) == []


def test_validate_spec_unknown_method():
    spec = replace(make_pv_spec(), model_method="data_periodic")
    errors = validate_spec(spec)
    assert any("model_method" in e for e in errors)


def test_validate_spec_stateful_requires_states():
    spec = replace(make_pv_spec(), stateful=True)
    errors = validate_spec(spec)
    assert any("stateful" in e for e in errors)


def test_validate_spec_states_only_when_stateful():
    spec = replace(make_battery_spec(), stateful=False)
    errors = validate_spec(spec)
    assert any("stateful" in e for e in errors)


# ---------------------------------------------------------------------------
# 机理基础函数(简单传热 / 功率平衡 / 设备出力,SI)
# ---------------------------------------------------------------------------


def test_heat_transfer_q():
    q = heat_transfer_q(100.0, np.array([300.0, 300.0]), np.array([290.0, 310.0]))
    np.testing.assert_allclose(q, [1000.0, -1000.0])


def test_power_balance():
    np.testing.assert_allclose(power_balance(5e4, 3e4), 2e4)
    np.testing.assert_allclose(power_balance(3e4, 5e4), -2e4)


def test_pv_output():
    ghi = np.array([0.0, 1000.0, 500.0])
    ta = np.full(3, 298.15)  # 25 °C
    p = pv_output(ghi, ta, rated_capacity_w=500000.0, efficiency=0.20)
    assert p[0] == 0.0  # 无辐照无出力
    # P = C·(G/G_STC)·(1 − β·(Tc−T_STC)),Tc = Ta + (NOCT−293.15)/800·G
    # 25 °C、1000 W/m²:Tc = 329.4 K → 温度项 0.875 → 437500 W
    np.testing.assert_allclose(p[1], 437500.0)
    # 25 °C、500 W/m²:温度项 0.9375 → 234375 W
    np.testing.assert_allclose(p[2], 234375.0)
    # 高温降额:50 °C、1000 W/m² → Tc = 354.4 K → 温度项 0.775 → 387500 W
    hot = pv_output(np.array([1000.0]), np.array([323.15]), 500000.0, efficiency=0.20)
    np.testing.assert_allclose(hot, 387500.0)


def test_pv_output_validation():
    with pytest.raises(ValueError):
        pv_output(np.array([1.0]), np.array([298.15, 298.15]), 1000.0)
    with pytest.raises(ValueError):
        pv_output(np.array([1.0]), np.array([np.nan]), 1000.0)


def test_heat_pump_cop_clamped():
    ta_cold = np.array([263.15])  # -10 °C
    ta_mid = np.array([283.15])  # 10 °C
    ta_hot = np.array([303.15])  # 30 °C
    cop_h = heat_pump_cop(np.concatenate([ta_cold, ta_mid, ta_hot]), "heating")
    assert cop_h[0] > 2.0  # 低温仍高于截断下限
    assert 3.0 < cop_h[1] < 5.0  # 中间段
    np.testing.assert_allclose(cop_h[2], 5.5)  # 高温钳制上限
    cop_c = heat_pump_cop(ta_mid, "cooling")
    assert 2.5 <= cop_c[0] <= 6.5


def test_boiler_chiller_gas():
    np.testing.assert_allclose(boiler_output(np.array([9e4]), 0.9), 1e5)
    np.testing.assert_allclose(chiller_output(np.array([1e4]), 4.0), 4e4)
    v = gas_volume_m3(np.array([3.6e9]), efficiency=0.9, lhv_j_per_m3=35.9e6)
    np.testing.assert_allclose(v, 3.6e9 / (0.9 * 35.9e6))


def test_simulate_battery_state_passing():
    # 无 state → 取 soc_initial
    soc1, state_new = simulate_battery(
        np.array([20000.0]), np.array([0.0]), capacity_j=100 * 3.6e6,
        soc_initial=0.5, charge_efficiency=0.95, soc_min=0.1, soc_max=0.9, dt_s=3600.0,
    )
    assert state_new["soc"] == pytest.approx(soc1[-1])
    assert 0.5 < soc1[-1] < 0.7  # 充电 20 kW × 1 h × 0.95 / 100 kWh ≈ +0.19
    # 传入 state dict(上一时间步末态)→ 继续递推
    soc2, state_new2 = simulate_battery(
        np.array([20000.0]), np.array([0.0]), capacity_j=100 * 3.6e6,
        soc_initial=0.5, charge_efficiency=0.95, soc_min=0.1, soc_max=0.9,
        state=state_new, dt_s=3600.0,
    )
    assert soc2[0] == pytest.approx(state_new["soc"])
    assert state_new2["soc"] > state_new["soc"]
    # 钳制:大充电功率下 SOC 封顶 max_soc
    soc3, state_new3 = simulate_battery(
        np.array([1e6]), np.array([0.0]), capacity_j=100 * 3.6e6,
        soc_initial=0.5, soc_min=0.1, soc_max=0.9, dt_s=3600.0,
    )
    assert soc3[-1] == pytest.approx(0.9)
    assert state_new3["soc"] == pytest.approx(0.9)


def test_simulate_battery_validation():
    with pytest.raises(ValueError):
        simulate_battery(np.array([1.0]), np.array([1.0, 2.0]), 1e6)
    with pytest.raises(ValueError):
        simulate_battery(np.array([1.0]), np.array([1.0]), -1.0)


# ---------------------------------------------------------------------------
# 机理命令生成与输入/输出 schema(字段名 + 单位)
# ---------------------------------------------------------------------------


def test_build_command_pv_schema():
    cmd, entry = build_command(make_pv_spec())
    assert isinstance(cmd, ModuleCommand)
    assert callable(entry)  # RR-P1-02: 无副作用构建返回统一 callable
    assert cmd.command_id == "ies.command.model.ies.device.pv.mechanism.1.4.0"
    assert cmd.function_ref == "iesplan.modeling.functions.pv_output"
    assert cmd.stateful is False
    assert cmd.version == "1.4.0"
    # 输入字段规格:参数 + 标准列,含单位
    input_names = [f.name for f in cmd.inputs]
    assert "rated_capacity_kwp" in input_names and "efficiency" in input_names
    assert "ghi" in input_names and "t_ambient" in input_names
    units = {f.name: f.unit for f in cmd.inputs}
    assert units["rated_capacity_kwp"] == "kWp"
    assert units["ghi"] == "W/m²"
    # 输出字段规格:out 端口(单位取 capacity_ref 参数单位)
    assert [f.name for f in cmd.outputs] == ["pv_out"]
    assert cmd.outputs[0].unit == "kWp"
    # RR-P1-02: 纯构建不注册任何全局状态(发布前不可见)
    assert get_command(cmd.command_id) is None
    assert cmd.command_id not in snapshot()
    # 发布后可见且 callable 与命令同快照
    from iesplan.modeling.command import replace_all_commands

    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: entry})
    assert get_command(cmd.command_id) is cmd
    assert cmd.command_id in snapshot()
    assert cmd in list_commands()


def test_build_command_mechanism_no_port_fallback_output():
    """无 out 端口声明时输出字段回退到机理映射默认(如热泵 COP,无量纲)。"""
    spec = DeviceSpec(
        type_id="ies.device.heat_pump",
        version="1.3.0",
        name_zh="热泵",
        name_en="Heat Pump",
        energy_carriers=["electric", "heat"],
        is_load=False,
        capabilities=["heat_pump"],
        parameters={"efficiency": _p("efficiency", "-", default=0.45)},
        model_method=MODEL_METHOD_MECHANISM,
        stateful=False,
        model_function="iesplan.modeling.functions.heat_pump_cop",
        time_series={"inputs": (SeriesSpec(key="t_ambient", unit="°C"),), "outputs": ()},
    )
    cmd, entry = build_command(spec)
    assert [f.name for f in cmd.outputs] == ["cop"]
    assert cmd.outputs[0].unit == "-"
    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: entry})
    result = call_command(
        cmd.command_id,
        {"params": {"efficiency": 0.45}, "series": {"t_ambient": np.array([283.15])},
         "state": None, "dt_s": 3600.0, "prices": {}},
    )
    assert result.outputs["cop"].shape == (1,)
    assert 2.0 < result.outputs["cop"][0] < 5.0


def test_build_command_mechanism_resolves_and_runs():
    cmd, _entry = build_command(make_pv_spec())
    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: _entry})
    ctx = {
        "params": {"rated_capacity_kwp": 500.0, "efficiency": 0.20},
        "series": {
            "ghi": np.full(24, 1000.0),
            "t_ambient": np.full(24, 298.15),
        },
        "state": None,
        "dt_s": 3600.0,
        "prices": {},
    }
    result = call_command(cmd.command_id, ctx)
    assert isinstance(result, DeviceRunResult)
    assert set(result.outputs) == {"pv_out"}
    # 500 kWp → 500000 W;25 °C、1000 W/m² 下 NOCT 温度修正 0.875 → 437500 W
    np.testing.assert_allclose(result.outputs["pv_out"], np.full(24, 437500.0))
    assert result.state_new is None  # 无状态模型不暴露状态


def test_resolve_function_ref():
    fn = resolve_function_ref("iesplan.modeling.functions.pv_output")
    assert callable(fn)
    with pytest.raises(NotFoundError):
        resolve_function_ref("iesplan.modeling.nonexistent.foo")
    with pytest.raises(NotFoundError):
        resolve_function_ref("not_a_path")


def test_call_command_unknown():
    with pytest.raises(NotFoundError):
        call_command("ies.command.model.ies.device.nope.mechanism.1.0.0", {})


def test_build_command_mechanism_whitelist():
    spec = replace(make_pv_spec(), model_function="os.system")
    with pytest.raises(ModelingConfigError):
        build_command(spec)


def test_build_command_mechanism_unknown_function():
    spec = replace(make_pv_spec(), model_function="iesplan.modeling.functions.nonexistent_fn")
    with pytest.raises(ModelingConfigError):
        build_command(spec)


def test_build_command_stateful_flag_mismatch():
    # 无状态函数绑定到 stateful 设备
    spec = replace(make_pv_spec(), stateful=True, states=(StateSpec(key="t", unit="-"),))
    with pytest.raises(ModelingConfigError):
        build_command(spec)
    # 有状态函数绑定到 stateless 设备
    spec = replace(make_battery_spec(), stateful=False, states=())
    with pytest.raises(ModelingConfigError):
        build_command(spec)


def test_build_command_unknown_method():
    spec = replace(make_pv_spec(), model_method="data_periodic")
    with pytest.raises(ModelingConfigError):
        build_command(spec)


# ---------------------------------------------------------------------------
# 数据方法-周期重复(periodic repeat 时间序列)
# ---------------------------------------------------------------------------


def test_periodic_repeat_extension():
    profile = {"e_load": np.arange(24, dtype=np.float64) + 1.0}
    out = periodic_repeat(profile, 50)
    assert set(out) == {"e_load"}
    assert out["e_load"].shape == (50,)
    np.testing.assert_array_equal(out["e_load"][:24], profile["e_load"])
    np.testing.assert_array_equal(out["e_load"][24:48], profile["e_load"])  # 整周期回绕
    np.testing.assert_array_equal(out["e_load"][48:], profile["e_load"][:2])  # 尾部截断
    # 多列一致扩展
    two = periodic_repeat({"a": np.zeros(24), "b": np.ones(24)}, 100)
    assert two["a"].shape == two["b"].shape == (100,)


def test_periodic_repeat_invalid():
    with pytest.raises(ValueError):
        periodic_repeat({"a": np.zeros(24)}, 0)
    with pytest.raises(ValueError):
        periodic_repeat({}, 100)
    with pytest.raises(ValueError):
        periodic_repeat({"a": np.zeros(24), "b": np.zeros(12)}, 100)


def test_build_command_data_repeat_and_run():
    spec = make_load_spec()
    curve = np.arange(24, dtype=np.float64) + 1.0  # 典型日曲线 1..24 kWh
    cmd, _entry = build_command(spec, profile={"e_load": curve})
    assert cmd.command_id == "ies.command.model.ies.device.electric_load.data_repeat.1.2.0"
    assert cmd.function_ref == "iesplan.modeling.datadriven.periodic_repeat"
    assert cmd.stateful is False
    assert cmd.data_file == "electric_load.csv"
    units = {f.name: f.unit for f in cmd.outputs}
    assert units["e_load_kw"] == "kW"
    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: _entry})
    # 调用:100 步时间轴 → 周期外推 × 容量缩放(peak_power_kw / 曲线峰值)
    ctx = {
        "params": {"peak_power_kw": 2400.0},  # 峰值 24 → 缩放 ×100
        "series": {"e_load": np.zeros(100)},
        "state": None,
        "dt_s": 3600.0,
        "prices": {},
    }
    result = call_command(cmd.command_id, ctx)
    assert result.outputs["e_load_kw"].shape == (100,)
    np.testing.assert_allclose(result.outputs["e_load_kw"][:24], curve * 100.0)
    np.testing.assert_allclose(result.outputs["e_load_kw"][24:48], curve * 100.0)
    assert result.state_new is None


def test_build_command_data_repeat_missing_profile():
    with pytest.raises(ModelingConfigError):
        build_command(make_load_spec(), profile=None)


def test_build_command_data_repeat_missing_input_column():
    spec = make_load_spec()
    with pytest.raises(ModelingConfigError):
        build_command(spec, profile={"wrong_col": np.zeros(24)})


# ---------------------------------------------------------------------------
# 数据方法-预测模型(stub 接口,阶段 B 实现)
# ---------------------------------------------------------------------------


def test_prediction_model_stub_raises():
    with pytest.raises(ModelingNotImplementedError):
        prediction_model("cop_model.onnx", {"t_ambient": np.zeros(24)})
    with pytest.raises(ModelingConfigError):
        prediction_model("", {"t_ambient": np.zeros(24)})
    with pytest.raises(ModelingConfigError):
        prediction_model("cop_model.onnx", {})


def test_build_command_data_predict_schema_and_stub():
    cmd, _entry = build_command(make_hp_forecast_spec())
    assert cmd.command_id == "ies.command.model.ies.device.heat_pump_dr.data_predict.1.0.0"
    assert cmd.function_ref == "iesplan.modeling.datadriven.prediction_model"
    assert cmd.stateful is True
    assert cmd.data_file == "cop_model.onnx"
    input_names = [f.name for f in cmd.inputs]
    assert "t_ambient" in input_names and "h_load" in input_names
    output_units = {f.name: f.unit for f in cmd.outputs}
    assert output_units["cop"] == "-"
    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: _entry})
    # 调用 stub:抛 ModelingNotImplementedError(禁止静默降级)
    with pytest.raises(ModelingNotImplementedError):
        call_command(cmd.command_id, {"params": {}, "series": {"t_ambient": np.zeros(24),
                                                               "h_load": np.zeros(24)}})


def test_build_command_data_predict_missing_model_file():
    spec = replace(make_hp_forecast_spec(), model_file=None)
    with pytest.raises(ModelingConfigError):
        build_command(spec)


# ---------------------------------------------------------------------------
# 有状态模型:状态输入/输出暴露与传递(电池 SOC)
# ---------------------------------------------------------------------------


def test_build_command_battery_state_schema():
    cmd, _entry = build_command(make_battery_spec())
    assert cmd.stateful is True
    # 状态字段规格(名称 + 单位 + 上下限取自参数 min/max)
    state_units = {f.name: f.unit for f in cmd.state_fields}
    assert state_units["soc"] == "-"
    soc_field = next(f for f in cmd.state_fields if f.name == "soc")
    assert soc_field.min == 0.0 and soc_field.max == 1.0
    # 输入字段含容量/效率参数(业务单位)
    units = {f.name: f.unit for f in cmd.inputs}
    assert units["capacity_kwh"] == "kWh"
    assert units["charge_efficiency"] == "-"
    # 输出字段:out 端口 bat_out
    assert [f.name for f in cmd.outputs] == ["bat_out"]


def test_call_command_battery_state_roundtrip():
    cmd, _entry = build_command(make_battery_spec())
    replace_all_commands({cmd.command_id: cmd}, generated={cmd.command_id: _entry})
    base_ctx = {
        "params": {"capacity_kwh": 100.0, "initial_soc": 0.5, "charge_efficiency": 0.95,
                   "discharge_efficiency": 0.95, "min_soc": 0.1, "max_soc": 0.9},
        "series": {"charge_w": np.full(24, 20000.0), "discharge_w": np.zeros(24)},
        "dt_s": 3600.0,
        "prices": {},
    }
    # 第一段:无 state → 以 initial_soc 初始化,输出首位=状态快照 + n 步末态
    r1 = call_command(cmd.command_id, {**base_ctx, "state": None})
    assert r1.state_new is not None and "soc" in r1.state_new
    assert r1.outputs["bat_out"].shape == (25,)  # n+1: 首位=初始状态快照 + 24 步末态
    np.testing.assert_allclose(r1.outputs["bat_out"][0], 0.5)  # 初始 SOC 快照
    assert r1.outputs["bat_out"][-1] == pytest.approx(r1.state_new["soc"])  # 末态回写
    # 第二段:把上一段 state_new 作为 state 输入 → 状态连续传递(SOC 单调不减)
    r2 = call_command(cmd.command_id, {**base_ctx, "state": r1.state_new})
    assert r2.outputs["bat_out"][0] == pytest.approx(r1.state_new["soc"])
    assert r2.state_new["soc"] >= r1.state_new["soc"]
    # 24 h × 20 kW × 0.95 = 456 kWh 远超 90 kWh 上限 → 封顶 max_soc
    np.testing.assert_allclose(r2.state_new["soc"], 0.9)


def test_mechanism_functions_table():
    # 机理映射表覆盖三类(简单传热/功率平衡/电池有状态)入口
    assert set(MECHANISM_FUNCTIONS) >= {
        "pv_output", "heat_transfer_q", "power_balance", "simulate_battery",
        "transport_pipe",
    }
    assert MECHANISM_FUNCTIONS["simulate_battery"].state_key == "soc"
    assert MECHANISM_FUNCTIONS["simulate_battery"].takes_dt is True
    # RR-P2-05: 传输管道 stateful 机理函数(state_key + takes_dt + 元组返回)。
    spec = MECHANISM_FUNCTIONS["transport_pipe"]
    assert spec.state_key == "delay_buffer"
    assert spec.takes_dt is True

    # 运行期实测: 传入 dt_s 不抛 TypeError, 返回 (out, state_new)。
    from iesplan.modeling.functions import transport_pipe

    out, state_new = transport_pipe(
        np.array([100.0, 200.0, 300.0]),
        loss_rate=0.1,
        state=None,
        dt_s=3600.0,
    )
    assert out.shape == (3,)
    assert np.allclose(out, [0.0, 0.0, 0.0])  # 首调用无缓存 → 出流全 0
    assert isinstance(state_new, dict)
    assert "delay_buffer" in state_new
    # 二次调用: 缓存上一轮的入流; rolled + 首位覆盖 0 后 × (1 - loss_rate)。
    out2, state_new2 = transport_pipe(
        np.array([400.0, 500.0, 600.0]),
        loss_rate=0.1,
        state=state_new,
        dt_s=3600.0,
    )
    # cached=[100,200,300]; shifted=roll(...,1)=[300,100,200]; shifted[0]=0 → [0,100,200]; ×0.9
    expected = np.array([0.0, 100.0, 200.0]) * 0.9
    assert np.allclose(out2, expected)


def test_register_command_override():
    spec = make_pv_spec()
    cmd, _entry = build_command(spec)
    cid = cmd.command_id
    replace_all_commands({cid: cmd}, generated={cid: _entry})
    assert len(list_commands()) == 1
    # 覆盖注册同 id:版本不变,命令表仍单条
    register_command(ModuleCommand(command_id=cid, function_ref="iesplan.modeling.functions.pv_output",
                                   version="1.4.0"))
    assert len(list_commands()) == 1
    assert get_command(cid).function_ref == "iesplan.modeling.functions.pv_output"


# ---------------------------------------------------------------------------
# RR-P1-02 验收: 真实设备经 register_catalog_commands 发布后 call_command 可执行
# ---------------------------------------------------------------------------


def _build_real_commands():
    """用真实内置 catalog 构建命令并原子发布; 返回 (command_id → ModuleCommand)。"""
    from iesplan.devices import init_registry
    from iesplan.modeling.registry_loader import register_catalog_commands

    init_registry()  # 加载真实 9 台设备(含 csv/价格)
    register_catalog_commands()
    return {c.command_id: c for c in list_commands()}


def test_catalog_register_mechanism_call():
    """mechanism 命令(光伏)经公开门面发布后 call_command 真正执行成功。"""
    commands = _build_real_commands()
    cmd = commands["ies.command.model.ies.device.pv.mechanism.1.4.0"]
    assert cmd.stateful is False
    ctx = {
        "params": {"rated_capacity_kwp": 500.0, "efficiency": 0.20},
        "series": {"ghi": np.full(24, 1000.0), "t_ambient": np.full(24, 298.15)},
        "state": None,
        "dt_s": 3600.0,
        "prices": {},
    }
    result = call_command(cmd.command_id, ctx)
    assert set(result.outputs) == {"pv_out"}
    assert result.outputs["pv_out"].shape == (24,)
    np.testing.assert_allclose(result.outputs["pv_out"], np.full(24, 437500.0))


def test_catalog_register_stateful_call():
    """stateful 命令(电池)发布后 call_command 执行并回写状态。"""
    commands = _build_real_commands()
    cmd = commands["ies.command.model.ies.device.battery.mechanism.1.5.0"]
    assert cmd.stateful is True
    ctx = {
        "params": {"capacity_kwh": 100.0, "initial_soc": 0.5},
        "series": {"charge_w": np.full(24, 20000.0), "discharge_w": np.zeros(24)},
        "state": None,
        "dt_s": 3600.0,
        "prices": {},
    }
    r1 = call_command(cmd.command_id, ctx)
    assert r1.state_new is not None and "soc" in r1.state_new
    r2 = call_command(cmd.command_id, {**ctx, "state": r1.state_new})
    assert r2.outputs["bat_out"][0] == pytest.approx(r1.state_new["soc"])


def test_catalog_register_data_repeat_call():
    """data_repeat 命令(电负荷)发布后 call_command 周期外推执行成功。"""
    commands = _build_real_commands()
    cmd = commands["ies.command.model.ies.device.electric_load.data_repeat.1.2.0"]
    assert cmd.stateful is False
    assert cmd.data_file is not None  # 标准 csv 已随描述导出并读入 profile
    ctx = {
        "params": {"peak_power_kw": 2400.0},
        "series": {"e_load": np.zeros(100)},
        "state": None,
        "dt_s": 3600.0,
        "prices": {},
    }
    result = call_command(cmd.command_id, ctx)
    # 输出键 = yaml time_series.outputs[0] 或标准 csv 首列(electric_load.csv → e_load)
    assert "e_load" in result.outputs
    assert result.outputs["e_load"].shape == (100,)
    assert float(result.outputs["e_load"][0]) > 0.0  # 曲线外推非零


def test_catalog_failure_preserves_old_snapshot_itemwise(monkeypatch: pytest.MonkeyPatch):
    """第 N 个候选失败后, 旧快照逐项完全相等(命令与 callable 均不可变)。

    覆盖真实场景: good 设备能成功构建(mechanism 函数可解析), bad 设备在
    其后构建失败 —— 验证前面成功构建的候选没有提前泄漏到全局注册表,
    旧命令表与 callable 表均逐项一致。
    """
    from iesplan.devices import DeviceModelDescriptor
    from iesplan.modeling import registry_loader
    from iesplan.modeling.command import _current_snapshot

    # 先发布一个"旧"快照(命令 + 生成 callable)
    old_cmd, old_entry = build_command(make_pv_spec())
    replace_all_commands({old_cmd.command_id: old_cmd}, generated={old_cmd.command_id: old_entry})

    before_commands = dict(_current_snapshot().commands)
    before_generated = dict(_current_snapshot().generated)

    good = DeviceModelDescriptor(
        type_id="ies.device.pv2", version="1.0.0", name_zh="光伏2", name_en="PV2",
        model_method="mechanism", stateful=False, fidelity="medium",
        energy_carriers=("solar", "electric"), is_load=False,
        capabilities=("pv",), extends="ies.device.base", help_topic="",
        parameters={}, ports=(), time_series={}, states=(),
        model_commands={"pv": "ies.model-command.pv.generation@1.0.0"},
    )
    bad = DeviceModelDescriptor(
        type_id="ies.device.bogus", version="1.0.0", name_zh="坏设备", name_en="Bad",
        model_method="mechanism", stateful=False, fidelity="medium",
        energy_carriers=("electric",), is_load=False,
        capabilities=("pv",), extends="ies.device.base", help_topic="",
        parameters={}, ports=(), time_series={}, states=(),
        model_commands={"pv": "ies.model-command.unknown.fn@1.0.0"},
    )
    monkeypatch.setattr(registry_loader, "list_device_descriptors", lambda: [good, bad])
    with pytest.raises(AppError):
        registry_loader.register_catalog_commands()

    # 旧快照逐项完全相等: 命令表与 callable 表均与发布前一致, 无半成品
    assert dict(_current_snapshot().commands) == before_commands
    assert dict(_current_snapshot().generated) == before_generated
    assert _current_snapshot().generated[old_cmd.command_id] is old_entry
    # good 成功构建但不泄漏到全局快照; bad 因函数不可解析被拒
    assert "ies.command.model.ies.device.pv2.mechanism.1.0.0" not in _current_snapshot().commands
    assert "ies.command.model.ies.device.bogus.mechanism.1.0.0" not in _current_snapshot().commands


def test_catalog_failure_compute_command_unresolvable(monkeypatch: pytest.MonkeyPatch):
    """计算命令 function_ref 无法解析时, 整个注册流程拒绝且不发布新状态(codex 复审 B3)。"""
    from iesplan.modeling import registry_loader
    from iesplan.modeling.command import _current_snapshot, compute_command_refs

    # 先发布一个"旧"快照(命令 + 生成 callable)
    old_cmd, old_entry = build_command(make_pv_spec())
    replace_all_commands({old_cmd.command_id: old_cmd}, generated={old_cmd.command_id: old_entry})
    before_commands = dict(_current_snapshot().commands)
    before_generated = dict(_current_snapshot().generated)

    # 注入一个无法解析的计算命令引用(模块存在但函数不存在)
    bad_refs = dict(compute_command_refs())
    bad_refs["ies.command.compute.unresolvable.v1"] = "iesplan.modeling.functions.nonexistent_fn"
    monkeypatch.setattr(registry_loader, "compute_command_refs", lambda: bad_refs)
    monkeypatch.setattr(
        registry_loader, "list_device_descriptors", lambda: []
    )
    with pytest.raises(AppError):
        registry_loader.register_catalog_commands()

    # 旧快照逐项完全相等, 无半成品(含计算命令)
    assert dict(_current_snapshot().commands) == before_commands
    assert dict(_current_snapshot().generated) == before_generated
    assert "ies.command.compute.unresolvable.v1" not in _current_snapshot().commands


def _descriptor(
    type_id: str,
    *,
    capabilities: tuple[str, ...],
    model_commands: dict[str, str],
    model_method: str = "mechanism",
) -> "DeviceModelDescriptor":
    """构造公开设备描述(绕过 YAML 解析, 直接进入建模注册流程)。"""
    from iesplan.devices import DeviceModelDescriptor  # noqa: PLC0415

    return DeviceModelDescriptor(
        type_id=type_id,
        version="1.0.0",
        name_zh="测试设备",
        name_en="Test Device",
        model_method=model_method,
        stateful=False,
        fidelity="medium",
        energy_carriers=("electric",),
        is_load=False,
        capabilities=capabilities,
        extends="ies.device.base",
        help_topic="",
        parameters={},
        ports=(),
        time_series={},
        states=(),
        model_commands=model_commands,
    )


def _register_one(monkeypatch: pytest.MonkeyPatch, desc) -> None:
    """仅注册单台设备(monkeypatch 公开门面), 断言抛 AppError。"""
    from iesplan.modeling import registry_loader

    monkeypatch.setattr(registry_loader, "list_device_descriptors", lambda: [desc])
    with pytest.raises(AppError) as exc:
        registry_loader.register_catalog_commands()
    assert exc.value.code == "SYS-CFG-001"
    assert exc.value.params.get("device_id") == desc.type_id


def test_catalog_rejects_command_version_mismatch(monkeypatch: pytest.MonkeyPatch):
    """声明命令版本与 provider 注册版本不一致 → AppError(SYS-CFG-001) 阻断发布。

    修复前 ref.split("@", 1)[0] 丢弃声明版本, 不比对即注册成功。
    """
    desc = _descriptor(
        "ies.device.pv_wrongver",
        capabilities=("pv",),
        model_commands={"pv": "ies.model-command.pv.generation@9.9.9"},
    )
    _register_one(monkeypatch, desc)


def test_catalog_rejects_unknown_command(monkeypatch: pytest.MonkeyPatch):
    """未知命令 ID → AppError(SYS-CFG-001) 阻断发布(而非只校验首个映射)。"""
    desc = _descriptor(
        "ies.device.unknown_cmd",
        capabilities=("pv",),
        model_commands={"pv": "ies.model-command.unknown.fn@1.0.0"},
    )
    _register_one(monkeypatch, desc)


def test_catalog_rejects_capability_missing_command(monkeypatch: pytest.MonkeyPatch):
    """capability 无对应 model_command → 逐 capability 解析拒绝, 不静默忽略。"""
    desc = _descriptor(
        "ies.device.missing_cap",
        capabilities=("pv", "mystery"),
        model_commands={"pv": "ies.model-command.pv.generation@1.0.0"},
    )
    _register_one(monkeypatch, desc)


def test_catalog_rejects_divergent_capability_commands(monkeypatch: pytest.MonkeyPatch):
    """capabilities 引用不同命令(无法表示为单一机理 provider) → 显式拒绝。"""
    desc = _descriptor(
        "ies.device.divergent",
        capabilities=("pv", "load"),
        model_commands={
            "pv": "ies.model-command.pv.generation@1.0.0",
            "load": "ies.model-command.load.periodic@1.0.0",
        },
    )
    _register_one(monkeypatch, desc)


def test_catalog_accepts_all_capabilities_same_command(monkeypatch: pytest.MonkeyPatch):
    """多 capability 指向同一命令(完整映射逐项校验一致) → 注册成功。"""
    from iesplan.modeling import registry_loader

    desc = _descriptor(
        "ies.device.ok_multi",
        capabilities=("pv", "controllable"),
        model_commands={
            "pv": "ies.model-command.pv.generation@1.0.0",
            "controllable": "ies.model-command.pv.generation@1.0.0",
        },
    )
    monkeypatch.setattr(registry_loader, "list_device_descriptors", lambda: [desc])
    registry_loader.register_catalog_commands()
    cmd = get_command("ies.command.model.ies.device.ok_multi.mechanism.1.0.0")
    assert cmd is not None
    assert cmd.function_ref == "iesplan.modeling.functions.pv_output"
