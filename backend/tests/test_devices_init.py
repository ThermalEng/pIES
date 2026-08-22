"""设备初始化模块测试(02 §7 验收: yaml 加载/校验/类型标志/价格缺省/标准 csv/注册表)。

- 类型标志按 05 §7.1 裁决: model_method(mechanism|data_repeat|data_predict) + stateful(bool);
- 价格缺省: $price: 引用解析、键缺失拒绝注册(错误含键名);
- 插件式: 新增设备 = 放入 yaml(+csv), 无需改代码。
"""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pytest import approx

from iesplan.core.errors import AppError, NotFoundError
from iesplan.devices import yamlmini
from iesplan.devices.loader import (
    DEFAULT_CATALOG_DIR,
    discover_device_dirs,
    load_all_devices,
    load_device_type,
    validate_device_dir,
)
from iesplan.devices.pricing import (
    PriceBook,
    algorithm_defaults,
    finance_defaults,
    get,
    load_price_book,
    resolve_param_default,
)
from iesplan.devices.profile import (
    extract_period_curve,
    make_template_csv,
    read_standard_csv,
    validate_series_csv,
)
from iesplan.devices.registry import DeviceRegistry, get_registry, init_registry
from iesplan.devices.spec import (
    MODEL_METHOD_LABELS,
    MODEL_METHODS,
    DeviceYamlSpec,
    load_yaml,
    spec_to_dict,
)

#: 合法机理设备基座(其余测试基于它做局部破坏) — 新 ies.device-model 1.0.0 格式
_BASE = """\
schema: ies.device-model
schema_version: "1.0.0"

device:
  id: ies.device.test
  version: "1.0.0"
  names:
    zh-CN: 测试设备
    en-US: Test Device
  model_method: mechanism
  stateful: false
  fidelity: medium
  energy_carriers: [electric]
  capabilities: [controllable]

parameters:
  cap:
    value_type: number
    quantity: power
    unit: kW
    required: false
    default: 10
    minimum: 0
    maximum: 1000

ports:
  out:
    carrier: electric
    direction: out
    quantity: power
    unit: kW

data_inputs: {}

states: {}

model_commands:
  controllable: ies.model-command.heat_pump.operation@1.0.0

extensions: {}
"""


def _write(directory, name: str, text: str) -> Path:
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


def _diag_codes(tmp_path, book: PriceBook, text: str, name: str = "dev") -> list[str]:
    _write(tmp_path, f"{name}.yaml", text)
    return [d.code for d in validate_device_dir(tmp_path, book)]


@pytest.fixture()
def book() -> PriceBook:
    return load_price_book()


# ---------------------------------------------------------------------------
# 内置 yaml 子集解析器
# ---------------------------------------------------------------------------


class TestYamlMini:
    def test_basic_parse(self):
        text = """\
# 注释行
version: 1.0.0
nested:
  a: {x: 1, y: [a, b]}
list:
  - {k: v}
  - name: 直接
    val: 2
flag: true
nothing: null
quoted: "a:b"
unit: "-"
"""
        data = yamlmini.load(text)
        assert data["version"] == "1.0.0"
        assert data["nested"] == {"a": {"x": 1, "y": ["a", "b"]}}
        assert data["list"] == [{"k": "v"}, {"name": "直接", "val": 2}]
        assert data["flag"] is True
        assert data["nothing"] is None
        assert data["quoted"] == "a:b"
        assert data["unit"] == "-"

    def test_document_start_and_doc_marker(self):
        text = "---\nversion: 1.0.0\n# 尾部注释\n"
        assert yamlmini.load(text) == {"version": "1.0.0"}

    def test_unclosed_flow_raises(self):
        with pytest.raises(yamlmini.YamlParseError):
            yamlmini.load("ports: [unclosed")

    def test_tab_indent_raises(self):
        with pytest.raises(yamlmini.YamlParseError):
            yamlmini.load("a:\n\tb: 1\n")


# ---------------------------------------------------------------------------
# 价格事实源 prices.yaml
# ---------------------------------------------------------------------------


class TestPriceBook:
    def test_load_default_catalog(self):
        book = load_price_book()
        assert isinstance(book, PriceBook)
        assert book.version == "1.0.0"
        assert book.currency == "CNY"
        assert book.energy_prices["electricity"]["import_tariff"] == {
            "peak": 1.1,
            "flat": 0.7,
            "valley": 0.3,
        }
        assert book.energy_prices["electricity"]["export_tariff"] == 0.35
        assert book.energy_prices["grid_emission_factor"] == 0.581
        assert book.device_costs["pv"]["unit_invest_cost"] == 3500
        assert book.device_costs["battery"]["lifetime_years"] == 10
        assert book.finance["tax_rate"] == 0.25
        assert book.finance["discount_rate"] == 0.08
        assert book.emissions["natural_gas"] == 0.202
        assert book.algorithm["ies.algo.milp_hybrid"]["gap_rel"] == 0.001

    def test_get_dotted_path(self):
        book = load_price_book()
        assert get(book, "device_costs.pv.unit_invest_cost") == 3500
        assert get(book, "energy_prices.electricity.import_tariff.peak") == 1.1

    def test_get_missing_key_raises_not_found(self):
        book = load_price_book()
        with pytest.raises(NotFoundError) as exc:
            get(book, "device_costs.nope.unit_invest_cost")
        assert exc.value.params["key"] == "device_costs.nope.unit_invest_cost"

    def test_finance_and_algorithm_defaults(self):
        book = load_price_book()
        fin = finance_defaults(book)
        assert fin == {
            "tax_rate": 0.25,
            "discount_rate": 0.08,
            "project_years": 20,
            "depreciation_years": 10,
            "irr_floor": 0.08,
        }
        algo = algorithm_defaults(book)
        assert algo["ies.algo.milp_hybrid"]["time_limit_s"] == 600
        assert algo["ies.algo.mc_sampling"]["n_samples"] == 100

    def test_missing_required_section_rejected(self, tmp_path):
        path = _write(tmp_path, "prices.yaml", "version: 1.0.0\ncurrency: CNY\n")
        with pytest.raises(AppError) as exc:
            load_price_book(path)
        assert "finance" in exc.value.params.get("sections", [])
        assert exc.value.code == "SYS-CFG-001"


# ---------------------------------------------------------------------------
# yaml 结构解析与类型标志
# ---------------------------------------------------------------------------


class TestDeviceYamlParse:
    def test_load_yaml_pv(self):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "pv.yaml")
        assert isinstance(spec, DeviceYamlSpec)
        assert spec.type_id == "ies.device.pv"
        assert spec.version == "1.4.0"
        assert spec.model_method == "mechanism"
        assert spec.stateful is False
        assert spec.fidelity == "high"
        assert list(spec.energy_carriers) == ["solar", "electric"]
        assert spec.is_load is False
        assert list(spec.capabilities) == ["pv", "controllable", "optimization_variable"]
        assert [p.name for p in spec.ports] == ["solar_in", "electric_out"]
        assert spec.ports[1].capacity_ref == "rated_capacity_kwp"
        p = spec.parameters["rated_capacity_kwp"]
        assert p.unit == "kWp"
        assert p.is_optimizable is True
        assert p.stock_or_addition == "addition"
        assert spec.parameters["efficiency"].default == 0.20
        assert spec.parameters["unit_invest_cost"].unit == "CNY/kWp"
        assert spec.parameters["unit_invest_cost"].default == "$price:device_costs.pv.unit_invest_cost"
        ts = spec.time_series["inputs"]
        assert [(s.key, s.unit, s.required) for s in ts] == [("ghi", "W/m2", True), ("t_ambient", "°C", True)]
        # 命令引用为稳定 ID@版本, 不再有 function/package/entry
        assert spec.model_commands["pv"] == "ies.model-command.pv.generation@1.0.0"
        assert "function" not in spec_to_dict(spec)
        assert "iesplan" not in str(spec.model_commands)

    def test_load_yaml_battery_stateful(self):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "battery.yaml")
        assert spec.stateful is True
        assert spec.model_method == "mechanism"
        st = spec.states[0]
        assert st.key == "soc"
        assert st.unit == "-"
        assert st.initial_ref == "initial_soc"
        assert dict(st.bounds) == {"min_ref": "min_soc", "max_ref": "max_soc"}
        assert spec.parameters["cycle_life"].unit == "次"
        assert spec.parameters["min_soc"].default == 0.1
        assert spec.parameters["capacity_kwh"].is_optimizable is True

    def test_load_yaml_grid_connection_price_refs(self):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "grid_connection.yaml")
        assert spec.model_method == "mechanism"
        assert spec.stateful is False
        assert spec.parameters["import_tariff"].default == "$price:energy_prices.electricity.import_tariff"
        assert spec.parameters["voltage_level_kv"].enum == (0.4, 10, 35, 110)
        assert spec.parameters["voltage_level_kv"].default == 10

    def test_load_yaml_electric_load_data_repeat(self):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "electric_load.yaml")
        assert spec.model_method == "data_repeat"
        assert spec.stateful is False
        assert spec.is_load is True
        s = spec.time_series["inputs"][0]
        assert (s.key, s.unit, s.resolution, s.required, s.period) == ("e_load", "kWh", "1h", True, "day")

    def test_load_yaml_heat_pump(self):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "heat_pump.yaml")
        assert spec.model_method == "mechanism"
        assert spec.parameters["source_type"].enum == ("air", "ground", "water")
        assert spec.parameters["unit_invest_cost"].default == "$price:device_costs.heat_pump.unit_invest_cost"

    def test_model_method_enum_and_labels(self):
        assert MODEL_METHODS == ("mechanism", "data_repeat", "data_predict")
        assert MODEL_METHOD_LABELS["mechanism"] == "机理模型"
        assert MODEL_METHOD_LABELS["data_repeat"] == "数据-周期重复"
        assert MODEL_METHOD_LABELS["data_predict"] == "数据-预测"

    @pytest.mark.parametrize(
        "patch, fragment",
        [
            ("id: ies.device.test", "id: bad_id!"),
            ('version: "1.0.0"', "version: v1"),
            ("model_method: mechanism", "model_method: data_periodic"),  # 02 旧命名废止
            ("model_method: mechanism", "model_method: data_forecast"),  # 02 旧命名废止
            ("stateful: false", "stateful: maybe"),
            ("energy_carriers: [electric]", "energy_carriers: []"),
            ("fidelity: medium", "fidelity: ultra"),
        ],
    )
    def test_load_yaml_rejects_invalid(self, tmp_path, patch, fragment):
        text = _BASE.replace(patch, fragment)
        with pytest.raises(AppError) as exc:
            load_yaml(_write(tmp_path, "bad.yaml", text))
        assert exc.value.code == "SYS-CFG-001"

    def test_load_yaml_param_errors(self, tmp_path):
        bad_params = [
            "    minimum: 10\n    maximum: 1",  # minimum > maximum
            "    bogus: 1",  # 未知键
            "    stock_or_addition: both",
        ]
        for bad in bad_params:
            # 在 cap 参数块内注入非法内容
            text = _BASE.replace(
                "    minimum: 0\n    maximum: 1000", bad
            )
            with pytest.raises(AppError):
                load_yaml(_write(tmp_path, "bad.yaml", text))

    def test_load_yaml_bad_series_resolution(self, tmp_path):
        # 新格式 data_inputs 无 resolution 字段, 结构非法由 schema 拒绝
        text = _BASE.replace(
            "data_inputs: {}\n",
            "data_inputs:\n  e_load:\n    value_type: number\n    quantity: power\n    unit: kW\n    required: true\n    resolution: 5h\n",
        )
        with pytest.raises(AppError):
            load_yaml(_write(tmp_path, "bad.yaml", text))

    def test_load_yaml_syntax_error(self, tmp_path):
        text = _BASE.replace("ports:", "ports: [unclosed")
        with pytest.raises(AppError) as exc:
            load_yaml(_write(tmp_path, "bad.yaml", text))
        assert exc.value.code == "SYS-CFG-001"
        assert "line" in exc.value.params

    def test_spec_to_dict_and_model_descriptor(self):
        """YAML 设备规格 → spec_to_dict 字典 + to_model_descriptor 公开建模描述。"""
        spec = load_yaml(DEFAULT_CATALOG_DIR / "pv.yaml")
        data = spec_to_dict(spec)
        assert data["model_method"] == "mechanism"
        assert data["stateful"] is False
        assert data["parameters"]["rated_capacity_kwp"]["unit"] == "kWp"
        assert data["ports"][1]["capacity_ref"] == "rated_capacity_kwp"
        # 公开建模描述直接来自 to_model_descriptor, 端口/命令完整透传
        from iesplan.devices.spec import to_model_descriptor
        desc = to_model_descriptor(spec)
        assert desc.type_id == "ies.device.pv"
        assert desc.version == "1.4.0"
        assert list(desc.energy_carriers) == ["solar", "electric"]
        assert desc.parameters["efficiency"].default == 0.20
        assert desc.help_topic == "help.modeling.pv"
        # YAML 真实端口完整透传
        assert any(p.name == "electric_out" and p.port_type == "electric" for p in desc.ports)
        # 公开描述不暴露函数/包/宿主机路径
        assert not hasattr(desc, "function")
        assert not hasattr(desc, "standard_csv_path")


# ---------------------------------------------------------------------------
# $price: 引用解析与价格缺省
# ---------------------------------------------------------------------------


class TestPriceResolution:
    def test_resolve_param_default_pv(self, book):
        spec = load_yaml(DEFAULT_CATALOG_DIR / "pv.yaml")
        resolved = resolve_param_default(spec, book)
        assert resolved == {"unit_invest_cost": 3500, "lifetime_years": 25}

    def test_missing_price_key_rejects_device(self, tmp_path, book):
        text = _BASE.replace(
            "    default: 10\n    minimum: 0\n    maximum: 1000",
            "    default: 10\n    minimum: 0\n    maximum: 1000\n"
            '  unit_cost:\n    value_type: number\n    quantity: power\n    unit: CNY/kW\n'
            '    required: false\n    default: "$price:device_costs.nope.unit_invest_cost"',
        )
        _write(tmp_path, "broken.yaml", text)
        diags = validate_device_dir(tmp_path, book)
        assert any(d.code == "SYS-CFG-001" for d in diags)
        with pytest.raises(AppError) as exc:
            load_all_devices(tmp_path, book)
        # 验收标准 2: 错误携带键名(经诊断 params.price_key)
        keys = {
            d["params"].get("price_key")
            for d in exc.value.params["diagnostics"]
        }
        assert "device_costs.nope.unit_invest_cost" in keys

    def test_plugin_new_device_no_code_change(self, tmp_path, book):
        """验收标准 1: 新增设备类型 = 放入 <type_id>.yaml, 无需改代码即被加载。"""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        text = _BASE.replace(
            "id: ies.device.test", "id: ies.device.plugin_dev"
        ).replace(
            "    default: 10\n    minimum: 0\n    maximum: 1000",
            "    default: 10\n    minimum: 0\n    maximum: 1000\n"
            '  unit_cost:\n    value_type: number\n    quantity: power\n    unit: CNY/kW\n'
            '    required: false\n    default: "$price:device_costs.gas_boiler.unit_invest_cost"',
        )
        _write(catalog, "plugin_dev.yaml", text)
        specs = load_all_devices(catalog, book)
        assert [s.type_id for s in specs] == ["ies.device.plugin_dev"]
        assert specs[0].parameters["unit_cost"].default == 600

    def test_any_device_failure_rejects_all(self, tmp_path, book):
        """验收标准 2: 任一设备校验失败 → 整体拒绝加载(不部分生效)。"""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        _write(catalog, "good.yaml", _BASE)
        bad = _BASE.replace("id: ies.device.test", "id: ies.device.bad").replace(
            "    default: 10\n    minimum: 0\n    maximum: 1000",
            "    default: 10\n    minimum: 0\n    maximum: 1000\n"
            '  unit_cost:\n    value_type: number\n    quantity: power\n    unit: CNY/kW\n'
            '    required: false\n    default: "$price:device_costs.missing.unit_invest_cost"',
        )
        _write(catalog, "bad.yaml", bad)
        with pytest.raises(AppError) as exc:
            load_all_devices(catalog, book)
        assert exc.value.params["diagnostics"]


# ---------------------------------------------------------------------------
# 跨字段约束与联合校验
# ---------------------------------------------------------------------------


class TestCrossFieldValidation:
    @pytest.mark.parametrize(
        "target, replacement",
        [
            ("stateful: false", "stateful: true"),  # stateful 必须声明 states
            ("model_method: mechanism", "model_method: data_repeat"),  # data_repeat 缺 period + csv
            (
                "model_commands:\n  controllable: ies.model-command.heat_pump.operation@1.0.0",
                "",  # mechanism 缺 model_commands → capability 无命令
            ),
            (
                "model_commands:\n  controllable: ies.model-command.heat_pump.operation@1.0.0",
                "model_commands:\n  controllable: ies.model-command.heat_pump.operation",  # 缺版本
            ),
            (
                "model_commands:\n  controllable: ies.model-command.heat_pump.operation@1.0.0",
                "model_commands:\n  controllable: iesplan.modeling.functions.pv_output@1.0.0",  # 模块路径泄露
            ),
            (
                "  out:\n    carrier: electric\n    direction: out\n    quantity: power\n    unit: kW",
                "  out:\n    carrier: electric\n    direction: out\n    quantity: power\n    unit: kW\n"
                "    capacity_parameter: nope",  # capacity_parameter 引用不存在参数
            ),
            (
                "  out:\n    carrier: electric\n    direction: out\n    quantity: power\n    unit: kW",
                "  out:\n    carrier: electric\n    direction: out\n    quantity: power\n    unit: kW\n"
                "  out2:\n    carrier: electric\n    direction: out\n    quantity: power\n    unit: kW\n"
                "    capacity_parameter: nope",  # 第二个端口引用不存在参数
            ),
        ],
    )
    def test_cross_field_rejects(self, tmp_path, book, target, replacement):
        text = _BASE.replace(target, replacement)
        codes = _diag_codes(tmp_path, book, text)
        assert "SYS-CFG-001" in codes

    def test_stateless_with_states_rejected(self, tmp_path, book):
        text = _BASE.replace(
            "states: {}\n",
            "states:\n  s:\n    unit: '-'\n",
        )
        codes = _diag_codes(tmp_path, book, text)
        assert "SYS-CFG-001" in codes

    def test_state_ref_unknown_param_rejected(self, tmp_path, book):
        text = _BASE.replace("stateful: false", "stateful: true").replace(
            "states: {}\n",
            "states:\n  soc:\n    unit: '-'\n    initial_ref: nope\n",
        )
        codes = _diag_codes(tmp_path, book, text)
        assert "SYS-CFG-001" in codes

    def test_data_repeat_requires_csv(self, tmp_path, book):
        text = _BASE.replace("model_method: mechanism", "model_method: data_repeat") + (
            "data_inputs:\n"
            "  e_load:\n    value_type: number\n    quantity: energy\n    unit: kWh\n    required: true\n"
        )
        codes = _diag_codes(tmp_path, book, text)
        assert "SYS-CFG-001" in codes  # 缺同名 csv + 缺 period

    def test_command_version_missing_rejected(self, tmp_path, book):
        text = _BASE.replace(
            "ies.model-command.heat_pump.operation@1.0.0",
            "ies.model-command.heat_pump.operation",
        )
        codes = _diag_codes(tmp_path, book, text)
        assert "SYS-CFG-001" in codes


# ---------------------------------------------------------------------------
# 目录发现与整体加载
# ---------------------------------------------------------------------------


class TestLoader:
    def test_discover_device_dirs(self, tmp_path):
        dirs = discover_device_dirs(DEFAULT_CATALOG_DIR)
        assert DEFAULT_CATALOG_DIR in dirs
        nested = tmp_path / "base"
        (nested / "sub").mkdir(parents=True)
        _write(nested / "sub", "dev.yaml", _BASE)
        assert discover_device_dirs(nested) == [nested / "sub"]

    def test_catalog_loads_clean(self, book):
        """内置 catalog: 10 台设备全部加载成功, 联合校验零诊断。

        RR-P2-05: 管道设备 ``ies.device.transport_pipe`` 作为合法业务设备
        进入 YAML 目录(单一事实源), 装配模块直接消费 descriptor。
        """
        specs = load_all_devices(DEFAULT_CATALOG_DIR, book)
        ids = [s.type_id for s in specs]
        assert ids == sorted(ids)
        assert ids == [
            "ies.device.battery",
            "ies.device.cooling_load",
            "ies.device.electric_chiller",
            "ies.device.electric_load",
            "ies.device.gas_boiler",
            "ies.device.grid_connection",
            "ies.device.heat_load",
            "ies.device.heat_pump",
            "ies.device.pv",
            "ies.device.transport_pipe",
        ]
        assert validate_device_dir(DEFAULT_CATALOG_DIR, book) == []

    def test_catalog_price_defaults_resolved(self, book):
        specs = {s.type_id: s for s in load_all_devices(DEFAULT_CATALOG_DIR, book)}
        pv = specs["ies.device.pv"]
        assert pv.parameters["unit_invest_cost"].default == 3500
        assert pv.parameters["lifetime_years"].default == 25
        for p in pv.parameters.values():
            assert not (isinstance(p.default, str) and p.default.startswith("$price:"))
        grid = specs["ies.device.grid_connection"]
        assert grid.parameters["import_tariff"].default == {"peak": 1.1, "flat": 0.7, "valley": 0.3}
        assert grid.parameters["export_tariff"].default == 0.35
        assert grid.parameters["demand_charge"].default == 40.0
        assert specs["ies.device.heat_pump"].parameters["unit_invest_cost"].default == 1800
        assert specs["ies.device.battery"].parameters["unit_invest_cost"].default == 900
        assert specs["ies.device.battery"].parameters["lifetime_years"].default == 10

    def test_load_device_type_single_and_multi(self, tmp_path, book):
        single = tmp_path / "single"
        single.mkdir()
        _write(single, "dev.yaml", _BASE)
        spec = load_device_type(single, book)
        assert spec.type_id == "ies.device.test"
        multi = tmp_path / "multi"
        multi.mkdir()
        _write(multi, "a.yaml", _BASE)
        _write(multi, "b.yaml", _BASE.replace("id: ies.device.test", "id: ies.device.test2"))
        with pytest.raises(AppError) as exc:
            load_device_type(multi, book)
        assert exc.value.params["count"] == 2


# ---------------------------------------------------------------------------
# 标准 csv 时间序列(profile.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec_electric_load():
    book = load_price_book()
    specs = load_all_devices(DEFAULT_CATALOG_DIR, book)
    return next(s for s in specs if s.type_id == "ies.device.electric_load")


def _series_spec(
    tmp_path, key: str, unit: str = "kWh", extra: str = "", name: str = "dev"
) -> DeviceYamlSpec:
    """构造带 data_inputs 声明的机理设备 spec(直接 load_yaml, 不做联合校验)。"""
    data_block = (
        "data_inputs:\n"
        f"  {key}:\n"
        f"    value_type: number\n"
        f"    quantity: power\n"
        f"    unit: {unit}\n"
        f"    required: true\n{extra}"
    )
    text = _BASE.replace("data_inputs: {}\n", data_block)
    return load_yaml(_write(tmp_path, f"{name}.yaml", text))


class TestProfileCsv:
    def test_read_catalog_csv(self, spec_electric_load):
        df = read_standard_csv(DEFAULT_CATALOG_DIR / "electric_load.csv", spec_electric_load)
        assert len(df) == 8760
        assert {"timestamp", "e_load"} <= set(df.columns)
        assert validate_series_csv(df, spec_electric_load) == []

    def test_read_csv_missing_required_column(self, tmp_path):
        spec = _series_spec(tmp_path, "e_load")
        csv_path = _write(tmp_path, "data.csv", "timestamp\n2025-01-01T00:00:00\n")
        with pytest.raises(AppError) as exc:
            read_standard_csv(csv_path, spec)
        assert exc.value.code == "DATA-COL-001"

    def test_validate_duplicate_timestamps(self, tmp_path):
        spec = _series_spec(tmp_path, "e_load")
        ts = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(8760)]
        ts[10] = ts[9]  # 制造相邻重复
        df = pd.DataFrame({"timestamp": ts, "e_load": [1.0] * 8760})
        codes = [d.code for d in validate_series_csv(df, spec)]
        assert "DATA-TS-001" in codes  # 重复

    def test_validate_out_of_order(self, tmp_path):
        spec = _series_spec(tmp_path, "e_load")
        ts = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(8760)]
        ts[10], ts[11] = ts[11], ts[10]  # 制造乱序
        df = pd.DataFrame({"timestamp": ts, "e_load": [1.0] * 8760})
        codes = [d.code for d in validate_series_csv(df, spec)]
        assert "DATA-TS-005" in codes  # 乱序

    def test_validate_range_out_of_bounds(self, tmp_path):
        spec = _series_spec(tmp_path, "e_load")
        df = pd.DataFrame(
            {
                "timestamp": [datetime(2025, 1, 1, h) for h in (0, 1, 2)],
                "e_load": [-5.0, 1.0, 2.0],
            }
        )
        codes = [d.code for d in validate_series_csv(df, spec)]
        assert "PARAM-RNG-003" in codes  # 越界

    def test_validate_non_numeric(self, tmp_path):
        spec = _series_spec(tmp_path, "e_load")
        df = pd.DataFrame(
            {
                "timestamp": [datetime(2025, 1, 1, h) for h in (0, 1, 2)],
                "e_load": ["abc", "1.0", "2.0"],
            }
        )
        codes = [d.code for d in validate_series_csv(df, spec)]
        assert "RES-NUM-001" in codes  # 非数值(与 core/diagnostics.RES_NUM_INVALID 一致)

    def test_validate_unit_mismatch_requires_convert(self, tmp_path):
        spec = _series_spec(tmp_path, "ghi", unit="kW")
        df = pd.DataFrame(
            {
                "timestamp": [datetime(2025, 1, 1, h) for h in (0, 1, 2)],
                "ghi": [600.0, 700.0, 800.0],
            }
        )
        codes = [d.code for d in validate_series_csv(df, spec)]
        assert "PARAM-UNIT-002" in codes  # 非标准单位直接拒绝

    def test_make_template_csv(self, spec_electric_load):
        text = make_template_csv(spec_electric_load, rows=3)
        lines = text.splitlines()
        assert lines[0].startswith("# pIES")
        data = [ln for ln in lines if not ln.startswith("#")]
        assert data[0] == "timestamp,e_load"
        assert len(data) == 4  # 表头 + 3 示例行
        assert any("# e_load,kWh,是" in ln for ln in lines)

    def test_extract_period_curve_day(self):
        n = 48
        ts = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(n)]
        vals = [float(i % 24 + 1) if i < 24 else float(i % 24 + 101) for i in range(n)]
        df = pd.DataFrame({"timestamp": ts, "e_load": vals})
        curve = extract_period_curve(df, "day")
        assert curve.shape == (24,)
        np.testing.assert_allclose(curve, [float(h + 51) for h in range(24)])


# ---------------------------------------------------------------------------
# 运行期注册表
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def registry():
    reg = DeviceRegistry(DEFAULT_CATALOG_DIR, load_price_book())
    reg.load()
    return reg


class TestRegistry:
    def test_load_get_list_snapshot(self, registry):
        assert len(registry.list()) == 10  # RR-P2-05: 含 transport_pipe 管道设备
        pv = registry.get("ies.device.pv")
        assert pv.version == "1.4.0"
        snap = registry.snapshot()
        assert "ies.device.pv@1.4.0" in snap
        assert all(len(x.split("@")) == 2 for x in snap)
        with pytest.raises(NotFoundError) as exc:
            registry.get("ies.device.nope")
        assert exc.value.code == "CONN-TYPE-002"

    def test_command_id(self, registry):
        """标准调用命令 id: 'ies.command.model.{type_id}.{method}.{version}'(03 §5.2)。"""
        assert registry.command_id("ies.device.pv") == "ies.command.model.ies.device.pv.mechanism.1.4.0"
        assert (
            registry.command_id("ies.device.electric_load")
            == "ies.command.model.ies.device.electric_load.data_repeat.1.2.0"
        )

    def test_port_directions(self, registry):
        assert registry.port_directions("ies.device.battery") == {"electric": "bidirectional"}
        assert registry.port_directions("ies.device.pv") == {"solar": "in", "electric": "out"}
        assert registry.port_directions("ies.device.heat_pump") == {
            "electric": "in",
            "heat": "out",
            "cool": "out",
        }
        assert registry.port_directions("ies.device.grid_connection") == {"electric": "out"}

    def test_coarse_category(self, registry):
        assert registry.coarse_category("ies.device.pv") == "pv"
        assert registry.coarse_category("ies.device.grid_connection") == "source"
        assert registry.coarse_category("ies.device.battery") == "storage"
        assert registry.coarse_category("ies.device.electric_load") == "load"
        assert registry.coarse_category("ies.device.heat_pump") == "converter"

    def test_entry_function_mechanism_binding(self, registry):
        """机理设备: 绑定 modeling 映射表函数, 返回统一契约 device_entry 可调用。"""
        fn = registry.get_entry_function("ies.device.pv")
        assert callable(fn)
        # 以统一契约调用: params(业务单位)+ series(内部单位) → DeviceRunResult
        import numpy as np

        from iesplan.modeling.command import DeviceRunResult

        n = 3
        result = fn(
            params={"rated_capacity_kwp": 100.0, "efficiency": 0.2},
            series={"ghi": np.full(n, 800.0), "t_ambient": np.full(n, 298.15)},
            state=None,
            dt_s=3600.0,
            prices={},
        )
        assert isinstance(result, DeviceRunResult)
        assert "pv_out" in result.outputs
        # 100 kWp × (800/1000) × (1−0.004×(298.15+45−298.15−…)) ≈ 72 kW = 72000 W
        # P = C·(G/G_STC)·[1−β·(Tc−T_STC)],Tc = Ta + (NOCT−293.15)/800·G
        assert float(result.outputs["pv_out"][0]) == approx(72000.0, rel=0.05)
        # 未注册设备 → 明确诊断
        with pytest.raises(AppError):
            registry.get_entry_function("ies.device.not_registered")

    def test_entry_function_battery_stateful(self, registry):
        """有状态设备(电池): 绑定 simulate_battery, state_new 回写下一状态。"""
        fn = registry.get_entry_function("ies.device.battery")
        assert callable(fn)

        import numpy as np

        result = fn(
            params={"capacity_kwh": 100.0, "initial_soc": 0.5},
            series={"charge_w": np.zeros(2), "discharge_w": np.zeros(2)},
            state=None,
            dt_s=3600.0,
            prices={},
        )
        assert result.state_new is not None and "soc" in result.state_new

    def test_reload_rejected_run_period(self, tmp_path, book):
        """RR-P2-04: 正式发布前不实现运行期热加载; reload 入口已删除。

        注册表仅在 ``init_registry`` 启动期加载一次, 运行期不再提供 reload。
        本测试断言该入口已下线(运行时调用 AttributeError)。
        """
        reg = DeviceRegistry(tmp_path, book)
        reg.load()
        assert reg.list() == []
        # 运行期不再提供 reload 入口(宪法 5.3)。
        assert not hasattr(reg, "reload")


class TestSingleton:
    def test_init_registry_singleton(self):
        reg = init_registry()  # 缺省内置 catalog + 缺省价格书
        assert reg is get_registry()
        assert len(reg.snapshot()) == 10  # RR-P2-05: 含 transport_pipe 管道设备
        reg2 = init_registry()  # 可重初始化
        assert reg2 is get_registry()


class TestInstalledPackageData:
    """RR-P1-01: 默认设备目录必须随安装包分发(Docker `pip install .[dev]` 后仍可加载)。

    这些测试不依赖仓库源码目录: 若 catalog 未进入 site-packages 的包资源
    (pyproject package-data 配置缺失), 以下断言必然失败。
    """

    def test_catalog_dir_lives_inside_installed_package(self):
        """catalog 目录必须位于 iesplan 包内(而非仓库源码路径)。"""
        import iesplan.devices.loader as loader_module

        pkg_root = Path(loader_module.__file__).resolve().parent  # .../iesplan/devices/
        expected = pkg_root / "catalog"
        assert DEFAULT_CATALOG_DIR == expected
        # 仓库源码目录可能恰好存在, 因此额外断言: 该目录属于包树内部
        assert str(DEFAULT_CATALOG_DIR).startswith(str(pkg_root.parent))

    def test_default_catalog_has_required_resources(self):
        """安装包内必须有 9 个设备 yaml + prices.yaml + 各 csv(直接按包路径读取)。"""
        yamls = sorted(DEFAULT_CATALOG_DIR.glob("*.yaml"))
        names = [p.stem for p in yamls]
        assert "prices" in names
        device_yamls = [p for p in yamls if p.stem != "prices"]
        assert len(device_yamls) == 10
        assert "pv" in names and "heat_pump" in names and "battery" in names
        for dev in ("electric_load", "heat_load", "cooling_load"):
            assert (DEFAULT_CATALOG_DIR / f"{dev}.csv").is_file()

    def test_import_loads_default_catalog_without_cwd_dependence(self, monkeypatch, tmp_path):
        """换工作目录后仍能经包路径加载默认目录并 init_registry 成功(RR-P1-01)。"""
        monkeypatch.chdir(tmp_path)  # 不依赖"源码目录恰好是当前工作目录"
        book = load_price_book()
        specs = load_all_devices(DEFAULT_CATALOG_DIR, book)
        assert len(specs) == 10
        assert validate_device_dir(DEFAULT_CATALOG_DIR, book) == []
        reg = DeviceRegistry(DEFAULT_CATALOG_DIR, book)
        reg.load()
        assert len(reg.snapshot()) == 10
