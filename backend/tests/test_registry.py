"""注册表单元测试:9 类设备齐全、参数 schema、算法注册、快照。"""

import pytest

from iesplan.core.errors import NotFoundError
from iesplan.core.registry import (
    AlgorithmSpec,
    DeviceTypeSpec,
    ParameterSpec,
    get_algorithm,
    get_device_type,
    list_algorithms,
    list_device_types,
    load_registry,
    snapshot,
)

#: 任务约定的 9 类设备(04 §3)
EXPECTED_DEVICE_TYPES = {
    "ies.device.grid_connection",
    "ies.device.pv",
    "ies.device.battery",
    "ies.device.electric_load",
    "ies.device.heat_load",
    "ies.device.cooling_load",
    "ies.device.heat_pump",
    "ies.device.gas_boiler",
    "ies.device.electric_chiller",
}


class TestDeviceRegistry:
    def test_all_nine_devices_registered(self):
        ids = {d.type_id for d in list_device_types()}
        assert EXPECTED_DEVICE_TYPES <= ids
        assert len(list_device_types()) >= 9

    def test_spec_fields(self):
        pv = get_device_type("ies.device.pv")
        assert isinstance(pv, DeviceTypeSpec)
        assert pv.version == "1.3.0"  # 04 §3.3
        assert "pv" in pv.capabilities
        assert pv.energy_carriers == ["solar", "electric"]
        assert pv.is_load is False
        assert pv.parameters["rated_capacity_kwp"].is_optimizable is True
        assert pv.parameters["rated_capacity_kwp"].stock_or_addition == "addition"
        assert pv.parameters["efficiency"].is_optimizable is False

    def test_load_types(self):
        assert get_device_type("ies.device.electric_load").is_load is True
        assert get_device_type("ies.device.heat_load").is_load is True
        assert get_device_type("ies.device.cooling_load").is_load is True
        assert get_device_type("ies.device.grid_connection").is_load is False

    def test_parameter_spec_complete(self):
        battery = get_device_type("ies.device.battery")
        p: ParameterSpec = battery.parameters["capacity_kwh"]
        assert p.unit == "kWh"
        assert p.min == 0
        assert p.max == 10_000_000
        assert p.default == 0
        assert p.is_optimizable is True
        assert p.help_key == "help.param.battery.capacity_kwh"
        # 存量默认:新增类容量参数存量默认 0(存量设备无新增容量)
        assert p.existing_default == 0
        # 存量类参数存量默认 = default
        assert battery.parameters["charge_efficiency"].existing_default == 0.95

    def test_grid_connection_params(self):
        grid = get_device_type("ies.device.grid_connection")
        assert grid.parameters["max_import_power_kw"].max == 200000
        assert grid.parameters["voltage_level_kv"].enum == (0.4, 10, 35, 110)
        assert grid.parameters["import_tariff"].default == {"peak": 1.1, "flat": 0.7, "valley": 0.3}

    def test_heat_pump_params(self):
        hp = get_device_type("ies.device.heat_pump")
        assert hp.parameters["cop"].min == 2.0
        assert hp.parameters["cop"].max == 6.5
        assert hp.parameters["cop"].default == 3.2
        assert hp.parameters["source_type"].enum == ("air", "ground", "water")
        assert hp.parameters["mode"].enum == ("heating", "cooling", "both")

    def test_load_params_reference(self):
        el = get_device_type("ies.device.electric_load")
        assert el.parameters["load_profile"].unit == "reference"
        assert el.parameters["load_profile"].default is None

    def test_unknown_type_raises(self):
        with pytest.raises(NotFoundError):
            get_device_type("ies.device.ufo")
        with pytest.raises(NotFoundError) as ei:
            get_device_type("ies.device.ufo")
        assert ei.value.code == "CONN-TYPE-002"


class TestAlgorithmRegistry:
    def test_default_algorithm(self):
        algo = get_algorithm("ies.algo.milp_hybrid")
        assert isinstance(algo, AlgorithmSpec)
        assert algo.version == "1.0.0"
        assert "milp" in algo.capabilities
        assert "capacity_design" in algo.capabilities
        assert algo.parameters["time_limit_s"].default == 600
        assert algo.parameters["seed"].default == 42

    def test_default_alias(self):
        a1 = get_algorithm("default")
        a2 = get_algorithm("ies.algo.default")
        a3 = get_algorithm("ies.algo.milp_hybrid")
        assert a1.algo_id == a2.algo_id == a3.algo_id == "ies.algo.milp_hybrid"

    def test_list_algorithms(self):
        ids = {a.algo_id for a in list_algorithms()}
        assert "ies.algo.milp_hybrid" in ids

    def test_unknown_algorithm_raises(self):
        with pytest.raises(NotFoundError):
            get_algorithm("ies.algo.not_exist")

    def test_default_parameters(self):
        algo = get_algorithm("ies.algo.milp_hybrid")
        defaults = algo.default_parameters
        assert defaults["seed"] == 42
        assert set(defaults) == set(algo.parameters)


class TestLoadFlow:
    """受控加载:静态注册幂等、版本校验、快照。"""

    def test_load_registry_idempotent(self):
        before = len(list_device_types())
        load_registry()  # 重复调用不产生重复
        load_registry()
        assert len(list_device_types()) == before

    def test_snapshot_format(self):
        snap = snapshot()
        assert "ies.device.pv@1.3.0" in snap
        assert "ies.algo.milp_hybrid@1.0.0" in snap
        # id@version 格式统一
        for item in snap:
            assert "@" in item

    def test_semver_validation(self):
        """非 semver 版本拒绝加载。"""
        from iesplan.core.errors import AppError
        from iesplan.core.registry import _check_version

        with pytest.raises(AppError):
            _check_version("1.0", "测试项")
        _check_version("1.0.0", "测试项")  # 合法不抛
