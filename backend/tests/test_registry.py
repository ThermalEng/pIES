"""注册表单元测试(RR-P2-02): 设备类型事实源已迁 YAML 目录(iesplan.devices)。

- TestDeviceRegistry: 9 类设备齐全、参数 schema、版本/能力/载体均来自 YAML,
  不再消费 core.registry 静态表;
- TestAlgorithmRegistry: 算法由 engines.registry 受控加载(RR-P2-02: 从 core.registry 迁出);
- TestLoadFlow: snapshot 混合 devices/algorithms, 设备版本以 YAML 为准。
"""

import pytest

from iesplan.core.errors import NotFoundError
from iesplan.devices import (
    get_device_descriptor,
    init_registry,
    list_device_descriptors,
)
from iesplan.devices.pricing import load_price_book
from iesplan.engines.registry import (
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
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


@pytest.fixture(scope="module", autouse=True)
def _init_device_yaml_registry():
    """启动期目录初始化(05 §3.2): 受控加载 + 价格解析, 后续测试直接消费。"""
    init_registry(book=load_price_book())


class TestDeviceRegistry:
    def test_all_nine_devices_registered(self):
        ids = {d.type_id for d in list_device_descriptors()}
        assert EXPECTED_DEVICE_TYPES <= ids
        assert len(list_device_descriptors()) >= 9

    def test_spec_fields(self):
        pv = get_device_descriptor("ies.device.pv")
        # 04 §3.3: pv 版本随 YAML 演进, 这里只断言格式合法
        assert pv.version.count(".") == 2
        assert "pv" in pv.capabilities
        assert list(pv.energy_carriers) == ["solar", "electric"]
        assert pv.is_load is False
        assert pv.parameters["rated_capacity_kwp"].is_optimizable is True
        assert pv.parameters["rated_capacity_kwp"].stock_or_addition == "addition"
        assert pv.parameters["efficiency"].is_optimizable is False

    def test_load_types(self):
        assert get_device_descriptor("ies.device.electric_load").is_load is True
        assert get_device_descriptor("ies.device.heat_load").is_load is True
        assert get_device_descriptor("ies.device.cooling_load").is_load is True
        assert get_device_descriptor("ies.device.grid_connection").is_load is False

    def test_parameter_spec_complete(self):
        battery = get_device_descriptor("ies.device.battery")
        p = battery.parameters["capacity_kwh"]
        assert p.unit == "kWh"
        assert p.min == 0
        assert p.max == 10_000_000
        assert p.default == 0
        assert p.is_optimizable is True
        assert p.help_key == "help.param.battery.capacity_kwh"
        # 新增类容量参数存量默认 0(存量设备无新增容量)
        assert p.existing_default == 0
        # 存量类参数存量默认 = default
        assert battery.parameters["charge_efficiency"].existing_default == 0.95

    def test_grid_connection_params(self):
        grid = get_device_descriptor("ies.device.grid_connection")
        assert grid.parameters["max_import_power_kw"].max == 200000
        assert grid.parameters["voltage_level_kv"].enum == (0.4, 10, 35, 110)
        # 价格引用已解析: import_tariff 来自价格册, YAML 原值为 "$price:..."
        assert isinstance(grid.parameters["import_tariff"].default, dict)

    def test_heat_pump_params(self):
        hp = get_device_descriptor("ies.device.heat_pump")
        assert hp.parameters["cop"].min == 2.0
        assert hp.parameters["cop"].max == 6.5
        assert hp.parameters["cop"].default == 3.2
        assert hp.parameters["source_type"].enum == ("air", "ground", "water")
        assert hp.parameters["mode"].enum == ("heating", "cooling", "both")

    def test_load_params_reference(self):
        el = get_device_descriptor("ies.device.electric_load")
        assert el.parameters["load_profile"].unit == "reference"

    def test_unknown_type_raises(self):
        with pytest.raises(NotFoundError) as ei:
            get_device_descriptor("ies.device.ufo")
        assert ei.value.code == "CONN-TYPE-002"

    def test_yaml_ports_published(self):
        """YAML 真实端口完整透传(RR-P1-04: 与前端 DTO 端口一一对应)。"""
        hp = get_device_descriptor("ies.device.heat_pump")
        names = {p.name for p in hp.ports}
        assert {"electric_in", "heat_out", "cool_out"} <= names
        battery = get_device_descriptor("ies.device.battery")
        # 电池端口双向: 端口名 = carrier, 单一载体
        assert any(p.direction == "bidirectional" for p in battery.ports)


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
    """快照(04 §2.2 规则 5):设备版本以 YAML 为准, 算法以 engines.registry 为准。"""

    def test_snapshot_format(self):
        # RR-P2-02: 设备快照与算法快照独立。设备经 iesplan.devices 公开快照;
        # 算法经 engines.registry。两者都需展示 @版本 格式。
        from iesplan.devices import list_device_descriptors
        device_snap = [f"{d.type_id}@{d.version}" for d in list_device_descriptors()]
        algo_snap = list_algorithms()
        snap = device_snap + [f"{a.algo_id}@{a.version}" for a in algo_snap]
        # 任一 pv 行存在(版本随 YAML 演进, 不固定 1.3.0)
        assert any(s.startswith("ies.device.pv@") for s in snap)
        assert "ies.algo.milp_hybrid@1.0.0" in snap
        # id@version 格式统一
        for item in snap:
            assert "@" in item

    def test_algorithm_snapshot_only(self):
        # 算法快照独立可用, 不引入 devices 导入(避免循环)
        snap = [f"{a.algo_id}@{a.version}" for a in list_algorithms()]
        assert "ies.algo.milp_hybrid@1.0.0" in snap
        assert all(s.startswith("ies.algo.") for s in snap)

    def test_semver_validation(self):
        """非 semver 版本拒绝加载。"""
        from iesplan.core.errors import AppError
        from iesplan.engines.registry import _check_version_external as _check_version

        with pytest.raises(AppError):
            _check_version("1.0", "测试项")
        _check_version("1.0.0", "测试项")  # 合法不抛