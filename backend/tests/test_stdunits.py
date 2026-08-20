"""单位标准化内核测试(审查意见第 0 条;方案 01)。

覆盖:
- §3.3 解析示例表全量(parse_quantity / parse_unit_string);
- 转换 API:normalize_unit / to_si / from_si / dims_of / unit_meta /
  assert_same_dims / convert / Quantity(§4.2);
- 边界:千分位/科学计数/中文乘数/context 兜底/复合连乘/大小写/别名冲突;
- 非法输入:坏数字/空串/括号/嵌套除法/仿射进复合/未注册单位/USD 折算/
  跨量纲换算。

纯 pytest,无数据库。说明:§3.3 表中 "30 C" 行结果列与说明列冲突(说明列注明
"宽松化:独立 C 允许"),按说明列实现——独立温度单位允许解析,进入复合拒绝。
"""

from __future__ import annotations

import pytest

from iesplan.core import units as base_units
from iesplan.core.stdunits import (
    AFFINE_UNITS,
    ALIAS_MAP,
    DATA_FIELD_UNITS,
    MULTIPLIERS,
    Quantity,
    UNITS,
    UnitError,
    UnitParseError,
    assert_same_dims,
    convert,
    dims_of,
    flow_unit_of,
    format_value,
    from_si,
    hourly_meta,
    is_known_unit,
    normalize_unit,
    parse_number,
    parse_quantity,
    parse_unit_string,
    si_unit_of,
    to_si,
    unit_meta,
)

approx = pytest.approx


# ---------------------------------------------------------------------------
# §3.3 解析示例表(parse_quantity 全量)
# ---------------------------------------------------------------------------
class TestParseQuantityExamples:
    """方案 01 §3.3 示例表逐行验证(字段:输入/context/期望 Quantity)。"""

    @pytest.mark.parametrize(
        ("text", "context", "value", "unit", "si_value", "si_unit"),
        [
            ("1000 kW", None, 1000, "kW", 1e6, "W"),
            ("1000kW", None, 1000, "kW", 1e6, "W"),
            ("1.5MWh", None, 1.5, "MWh", 5.4e9, "J"),
            ("3 元/kWh", None, 3, "CNY/kWh", 3 / 3.6e6, "CNY/J"),
            ("40 元/kW·月", None, 40, "CNY/kW·月", 40 / (1e3 * 2.592e6), "CNY/(W·s)"),
            ("2 tCO2/万m³", None, 2, "tCO2/万m³", 0.2, "kg/m³"),
            ("0.581 tCO2/MWh", None, 0.581, "tCO2/MWh", 0.581 * 1e3 / 3.6e9, "kg/J"),
            ("25℃", None, 25, "C", 298.15, "K"),
            ("25 °C", None, 25, "C", 298.15, "K"),
            ("10%", None, 10, "%", 0.1, "1"),
            ("1.5万", "kW", 15000, "kW", 1.5e7, "W"),
            ("1000", "kW", 1000, "kW", 1e6, "W"),
            ("0.35 元/kWh", None, 0.35, "CNY/kWh", 0.35 / 3.6e6, "CNY/J"),
            ("500 kWp", None, 500, "kW", 5e5, "W"),
            ("30 C", None, 30, "C", 303.15, "K"),  # 宽松化:独立 C 允许(见模块 docstring)
            ("30", "C", 30, "C", 303.15, "K"),
            ("3", "CNY/kWh", 3, "CNY/kWh", 3 / 3.6e6, "CNY/J"),
        ],
    )
    def test_example_table(self, text, context, value, unit, si_value, si_unit):
        q = parse_quantity(text, context=context)
        assert q.value == approx(value)
        assert q.unit == unit
        assert q.si_value == approx(si_value)
        assert q.si_unit == si_unit

    @pytest.mark.parametrize(
        "text",
        [
            "abc kW",  # 数字缺失
            "3 元/(kWh·h)",  # 括号拒绝
            "3 元/kWh/h",  # 嵌套除法拒绝
            "30 C/kW",  # 仿射单位进入分母拒绝
            "kW·C",  # 仿射单位进入复合拒绝
            "30 C/kW",  # 同上(分母)
            "0.5 万",  # 纯乘数无符号且无 context
            "",
            "   ",
            "kW",  # 无数字
            "1.5万kWh/h/m",  # 嵌套除法
            "2//m³",  # 空分子
        ],
    )
    def test_example_table_errors(self, text):
        with pytest.raises(UnitParseError):
            parse_quantity(text)

    def test_missing_unit_without_context_raises(self):
        with pytest.raises(UnitParseError) as ei:
            parse_quantity("1.5万")
        err = ei.value
        assert err.code == "PARAM-UNIT-001"
        assert err.message_key == "ies.diag.param.unit_parse"
        assert err.params["text"] == "1.5万"
        assert "position" in err.params
        assert "expected" in err.params
        assert "suggestions" in err.params

    def test_context_ignored_when_unit_present(self):
        assert parse_quantity("1000 kW", context="MW").si_value == approx(1e6)

    def test_error_position_absolute(self):
        with pytest.raises(UnitParseError) as ei:
            parse_quantity("1000 bogus")
        assert ei.value.params["position"] == 4


# ---------------------------------------------------------------------------
# 数值词法边界(千分位/科学计数/中文乘数/符号)
# ---------------------------------------------------------------------------
class TestNumberLexing:
    def test_thousands_separator(self):
        assert parse_quantity("1,000 kW").value == 1000
        assert parse_quantity("1,000,000 kWh").value == 1_000_000

    def test_decimal_with_separator(self):
        assert parse_quantity("1,000.5 kWh").value == approx(1000.5)

    def test_scientific(self):
        assert parse_quantity("2e3 kWh").value == 2000
        assert parse_quantity("1.5E-2 kW").si_value == approx(15.0)

    def test_sign(self):
        assert parse_quantity("-5 kW").si_value == approx(-5e3)
        assert parse_quantity("+5 kW").value == 5

    def test_chinese_multiplier_suffix_with_symbol(self):
        # "2万m³" 贪婪:数值 "2万"=20000 + 单位 m³
        q = parse_quantity("2万m³")
        assert q.value == 20000
        assert q.unit == "m³"
        assert q.si_value == approx(20000)

    def test_chinese_multiplier_as_token(self):
        q = parse_quantity("3 万元")
        assert q.value == 3
        assert q.unit == "万CNY"
        assert q.si_value == approx(30000)

    def test_10k_in_context(self):
        assert parse_quantity("0.5 万", context="kW").value == 5000

    def test_number_only_without_mult(self):
        assert parse_quantity("1000", context="kWh").si_value == approx(3.6e9)

    def test_multipliers_table(self):
        assert MULTIPLIERS == {"百": 1e2, "千": 1e3, "万": 1e4, "亿": 1e8}

    def test_parse_number(self):
        assert parse_number("1,000") == 1000
        assert parse_number("1.5万") == 15000
        assert parse_number("2e3") == 2000
        assert parse_number("-0.5") == -0.5


# ---------------------------------------------------------------------------
# parse_unit_string / si_unit_of
# ---------------------------------------------------------------------------
class TestParseUnitString:
    @pytest.mark.parametrize(
        ("s", "canonical", "factor"),
        [
            ("kWp", "kW", 1e3),
            ("元/kWh", "CNY/kWh", 1 / 3.6e6),
            ("CNY/kW·月", "CNY/kW·月", 1 / (1e3 * 2.592e6)),
            ("CNY/kW*月", "CNY/kW·月", 1 / (1e3 * 2.592e6)),  # * 连乘归一为 ·
            ("tCO2/万m³", "tCO2/万m³", 1e3 / 1e4),
            ("千m³", "千m³", 1e3),
            ("KWH", "kWh", 3.6e6),
            ("℃", "C", 1.0),
            ("%", "%", 0.01),
            ("-", "-", 1.0),
            ("kg/kWh", "kg/kWh", 1 / 3.6e6),
            ("kJ/m³", "kJ/m³", 1e3),
            ("W/m²", "W/m²", 1.0),
            ("月", "月", 2_592_000.0),
            ("d", "d", 86400.0),
            ("a", "a", 8760 * 3600.0),
            ("CNY/m³", "CNY/m³", 1.0),
            ("元/kWh", "CNY/kWh", 1 / 3.6e6),
            ("万m³", "万m³", 1e4),
            ("无量纲", "-", 1.0),
        ],
    )
    def test_canonical_and_factor(self, s, canonical, factor):
        assert parse_unit_string(s) == (canonical, approx(factor))

    @pytest.mark.parametrize(
        ("s", "si_unit"),
        [
            ("kW", "W"),
            ("MWh", "J"),
            ("CNY/kWh", "CNY/J"),
            ("CNY/kW·月", "CNY/(W·s)"),
            ("tCO2/万m³", "kg/m³"),
            ("tCO2/MWh", "kg/J"),
            ("W/m²", "W/m²"),
            ("C", "K"),
            ("%", "1"),
            ("-", "1"),
            ("kV", "V"),
            ("月", "s"),
        ],
    )
    def test_si_unit(self, s, si_unit):
        assert si_unit_of(parse_unit_string(s)[0]) == si_unit

    @pytest.mark.parametrize(
        "s",
        [
            "kW/kWh/h",
            "CNY/(kWh·h)",
            "CNY/kWh/",
            "/kWh",
            "kWh·",
            "·kWh",
            "kW··h",
            "bogus",
            "0.5 万",  # 单位串不允许数值
        ],
    )
    def test_parse_unit_string_errors(self, s):
        with pytest.raises(UnitParseError):
            parse_unit_string(s)

    def test_affine_in_compound_rejected(self):
        with pytest.raises(UnitParseError) as ei:
            parse_unit_string("C/kW")
        assert ei.value.code == "PARAM-UNIT-001"
        with pytest.raises(UnitParseError):
            parse_unit_string("kW·C")

    def test_standalone_affine_allowed(self):
        assert parse_unit_string("C")[0] == "C"
        assert parse_unit_string("F")[0] == "F"

    def test_overlong_unit_rejected(self):
        with pytest.raises(UnitParseError):
            parse_unit_string("kWh·" * 30)


# ---------------------------------------------------------------------------
# normalize_unit(§4.2)
# ---------------------------------------------------------------------------
class TestNormalizeUnit:
    @pytest.mark.parametrize(
        ("s", "expected"),
        [
            ("kWp", "kW"),
            ("kWp ", "kW"),
            ("MWp", "MW"),
            ("元/kWh", "CNY/kWh"),
            ("℃", "C"),
            ("°C", "C"),
            ("0.35元/kWh", "CNY/kWh"),
            ("25 ℃", "C"),
            ("KWH", "kWh"),
            ("kwh", "kWh"),
            ("tCO2/万m³", "tCO2/万m³"),
            ("CNY/kW·月", "CNY/kW·月"),
            ("度", "kWh"),  # 别名冲突先注册者优先(能量语义)
            ("1", "-"),
            ("无量纲", "-"),
            ("%", "%"),
            ("千克", "kg"),
            ("千克/kWh", "kg/kWh"),
            ("摄氏度", "C"),
        ],
    )
    def test_normalize(self, s, expected):
        assert normalize_unit(s) == expected

    @pytest.mark.parametrize(
        "s",
        ["", "bogus", "1000", "kW//h", "0.35", "kWp kWh"],
    )
    def test_normalize_errors(self, s):
        with pytest.raises(UnitError) as ei:
            normalize_unit(s)
        assert ei.value.code == "PARAM-UNIT-002"

    def test_normalize_is_idempotent(self):
        for s in ("kW", "CNY/kWh", "tCO2/万m³", "C", "%", "CNY/kW·月"):
            assert normalize_unit(normalize_unit(s)) == normalize_unit(s)


# ---------------------------------------------------------------------------
# to_si / from_si(§4.2,计算边界唯一换算入口)
# ---------------------------------------------------------------------------
class TestToSiFromSi:
    @pytest.mark.parametrize(
        ("value", "unit", "si"),
        [
            (1000, "kW", 1e6),
            (40, "CNY/kW·月", 40 / (1e3 * 2.592e6)),
            (25, "C", 298.15),
            (10, "%", 0.1),
            (2, "tCO2/万m³", 0.2),
            (1, "MWh", 3.6e9),
            (1, "d", 86400.0),
            (1, "月", 2_592_000.0),
            (1, "kWh", 3.6e6),
            (0.581, "tCO2/MWh", 0.581 * 1e3 / 3.6e9),
            (3, "元/kWh", 3 / 3.6e6),
            (0.5, "kg/kWh", 0.5 / 3.6e6),
            (1, "V", 1.0),
            (2, "kV", 2000.0),
            (1, "m³", 1.0),
            (1, "kWp", 1e3),
            (30, "C", 303.15),
        ],
    )
    def test_to_si(self, value, unit, si):
        assert to_si(value, unit) == approx(si)

    def test_from_si_round_trip(self):
        for value, unit in ((1000, "kW"), (40, "CNY/kW·月"), (2, "tCO2/万m³"), (1, "MWh"), (3, "元/kWh")):
            assert from_si(to_si(value, unit), unit) == approx(value)

    def test_from_si_affine(self):
        assert from_si(298.15, "C") == approx(25.0)
        assert from_si(77.0 * 5 / 9 + 459.67 * 5 / 9, "F") == approx(77.0)

    def test_usd_conversion_rejected(self):
        for fn in (lambda: to_si(1, "USD"), lambda: from_si(1, "USD"), lambda: convert(1, "CNY", "USD")):
            with pytest.raises(UnitError) as ei:
                fn()
            assert ei.value.code == "PARAM-UNIT-002"

    def test_to_si_unknown_unit(self):
        with pytest.raises(UnitError):
            to_si(1, "bogus")


# ---------------------------------------------------------------------------
# dims_of / assert_same_dims(§4.2,替换 config.py 精确查表)
# ---------------------------------------------------------------------------
class TestDims:
    @pytest.mark.parametrize(
        ("unit", "dims"),
        [
            ("kW", {"power": 1}),
            ("kWh", {"energy": 1}),
            ("CNY/kWh", {"currency": 1, "energy": -1}),
            ("CNY/kW", {"currency": 1, "power": -1}),
            ("CNY/kW·月", {"currency": 1, "power": -1, "time": -1}),
            ("tCO2/万m³", {"mass": 1, "volume": -1}),
            ("tCO2/MWh", {"mass": 1, "energy": -1}),
            ("kg/kWh", {"mass": 1, "energy": -1}),
            ("kJ/m³", {"energy": 1, "volume": -1}),
            ("W/m²", {"power": 1, "area": -1}),
            ("kV", {"voltage": 1}),
            ("C", {"temperature": 1}),
            ("%", {}),
            ("-", {}),
            ("月", {"time": 1}),
            ("s", {"time": 1}),
            ("a", {"time": 1}),
            ("CNY/m³", {"currency": 1, "volume": -1}),
            ("度", {"energy": 1}),  # 别名冲突:度 → kWh(能量)
            ("kWp", {"power": 1}),
            ("万m³", {"volume": 1}),
        ],
    )
    def test_dims_of(self, unit, dims):
        assert dict(dims_of(unit)) == dims

    def test_dims_of_dimensionless_empty_counter(self):
        assert dims_of("%") == {}

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("kW", "MW"),
            ("kWh", "MWh"),
            ("tCO2/MWh", "kg/kWh"),
            ("CNY/kWh", "元/kWh"),
            ("度", "kWh"),
            ("C", "K"),
            ("kWp", "kW"),
        ],
    )
    def test_assert_same_dims_ok(self, a, b):
        assert_same_dims(a, b)  # 不抛异常

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("kW", "kWh"),
            ("kWh", "kW"),
            ("kW", "CNY"),
            ("CNY/kWh", "CNY/kW"),
            ("tCO2/MWh", "tCO2/万m³"),
            ("kg/kWh", "W/m²"),
        ],
    )
    def test_assert_same_dims_mismatch(self, a, b):
        with pytest.raises(UnitError):
            assert_same_dims(a, b)


# ---------------------------------------------------------------------------
# convert(§4.2,超集:支持复合单位)
# ---------------------------------------------------------------------------
class TestConvert:
    @pytest.mark.parametrize(
        ("value", "frm", "to", "expected"),
        [
            (1000, "kW", "MW", 1.0),
            (3.6e6, "J", "kWh", 1.0),
            (1000, "kWh", "MWh", 1.0),  # 1 MWh = 1000 kWh
            (25, "C", "F", 77.0),
            (0.5, "tCO2/万m³", "kg/m³", 0.05),
            (3, "CNY/kWh", "CNY/MWh", 3000.0),
            (2, "kWp", "MW", 0.002),
            (1, "a", "d", 365.0),
            (3600, "s", "h", 1.0),
            (40, "CNY/kW·月", "CNY/kW·d", 40 / 30),  # 月=30d:日费率=月费率/30
        ],
    )
    def test_convert(self, value, frm, to, expected):
        assert convert(value, frm, to) == approx(expected)

    def test_convert_cross_dims_rejected(self):
        with pytest.raises(UnitError):
            convert(1, "kW", "kWh")

    def test_convert_unknown_rejected(self):
        with pytest.raises(UnitError):
            convert(1, "kW", "bogus")


# ---------------------------------------------------------------------------
# Quantity(§4.2)
# ---------------------------------------------------------------------------
class TestQuantity:
    def test_fields(self):
        q = parse_quantity("1000 kW")
        assert q == Quantity(value=1000, unit="kW", si_value=1e6, si_unit="W")

    def test_float_returns_si(self):
        assert float(parse_quantity("1000 kW")) == approx(1e6)
        assert float(parse_quantity("25℃")) == approx(298.15)

    def test_to(self):
        q = parse_quantity("1000 kW")
        assert q.to("MW") == approx(1.0)
        assert q.to("kW") == approx(1000.0)
        assert parse_quantity("25℃").to("K") == approx(298.15)
        assert parse_quantity("3 元/kWh").to("CNY/MWh") == approx(3000.0)

    def test_to_cross_dims_rejected(self):
        q = parse_quantity("1000 kW")
        with pytest.raises(UnitError):
            q.to("kWh")
        with pytest.raises(UnitError):
            q.to("J")

    def test_frozen(self):
        q = parse_quantity("1000 kW")
        with pytest.raises(Exception):
            q.value = 1  # frozen dataclass 拒绝赋值


# ---------------------------------------------------------------------------
# unit_meta(§4.2,前端渲染/校验用)
# ---------------------------------------------------------------------------
class TestUnitMeta:
    def test_single_unit(self):
        meta = unit_meta("kW")
        assert meta["unit"] == "kW"
        assert meta["category"] == "power"
        assert meta["si_unit"] == "W"
        assert meta["to_si"] == approx(1e3)
        assert meta["dims"] == {"power": 1}
        assert meta["precision_digits"] == 1
        assert meta["symbol_zh"] == "千瓦"
        assert meta["symbol_en"] == "kW"

    def test_percent(self):
        meta = unit_meta("%")
        assert meta["category"] == "dimensionless"
        assert meta["to_si"] == approx(0.01)
        assert meta["dims"] == {}

    def test_composite(self):
        meta = unit_meta("CNY/kWh")
        assert meta["category"] == "composite"
        assert meta["si_unit"] == "CNY/J"
        assert meta["to_si"] == approx(1 / 3.6e6)
        assert meta["dims"] == {"currency": 1, "energy": -1}

    def test_alias_input(self):
        assert unit_meta("元/kWh")["unit"] == "CNY/kWh"

    def test_unknown_raises(self):
        with pytest.raises(UnitError):
            unit_meta("bogus")


# ---------------------------------------------------------------------------
# 注册表与既有 core/units.py 兼容
# ---------------------------------------------------------------------------
class TestRegistryCompat:
    def test_extended_units_present(self):
        for uid in ("kg", "t", "tCO2", "m³", "千m³", "V", "kV", "MV", "m²", "-", "%", "d", "月"):
            assert uid in UNITS

    def test_base_units_untouched(self):
        # 不改写既有 core/units.py:新词条只出现在本包注册表
        assert "kg" not in base_units.UNITS
        assert "月" not in base_units.UNITS
        assert base_units.UNITS["kW"].to_si == 1e3

    def test_alias_map_extended(self):
        assert ALIAS_MAP["kwp"] == "kW"
        assert ALIAS_MAP["mwp"] == "MW"
        assert ALIAS_MAP["°c"] == "C"
        assert ALIAS_MAP["tco2"] == "tCO2"
        assert ALIAS_MAP["月"] == "月"
        # 冲突先注册者优先:度 → kWh(能量)而非 deg(角度)
        assert ALIAS_MAP["度"] == "kWh"

    def test_affine_units(self):
        assert AFFINE_UNITS == frozenset({"C", "F"})

    def test_is_known_unit(self):
        assert is_known_unit("kWh")
        assert is_known_unit("CNY/kWh")
        assert is_known_unit("kWp")
        assert is_known_unit("度")
        assert not is_known_unit("bogus")
        assert not is_known_unit("")

    def test_base_format_value_still_works(self):
        # 与既有 units.py 的展示函数兼容(金额 Decimal 重算语义不变)
        assert "千瓦时" in format_value(3.6, "kWh")
        assert "3.6 kW" in format_value(3.6, "kW", lang="en")

    def test_convert_via_base_alias(self):
        # 既有别名(度)在本扩展 convert 中同样生效
        assert convert(1, "度", "kWh") == approx(1.0)


# ---------------------------------------------------------------------------
# 数据字段单位契约(fields.py,裁决 7.5 归 0 层)
# ---------------------------------------------------------------------------
class TestFieldContract:
    def test_data_field_units(self):
        assert DATA_FIELD_UNITS["e_load"] == "kWh"
        assert DATA_FIELD_UNITS["h_load"] == "kWh"
        assert DATA_FIELD_UNITS["c_load"] == "kWh"
        assert DATA_FIELD_UNITS["t_ambient"] == "C"
        assert DATA_FIELD_UNITS["ghi"] == "W/m²"
        assert DATA_FIELD_UNITS["electricity_price"] == "CNY/kWh"
        assert DATA_FIELD_UNITS["grid_emission_factor"] == "kg/kWh"
        assert DATA_FIELD_UNITS["gas_price"] == "CNY/m³"

    def test_all_declared_units_are_normalizable(self):
        for field, unit in DATA_FIELD_UNITS.items():
            assert normalize_unit(unit) == unit, f"{field}: {unit}"

    def test_flow_unit_explicit(self):
        assert flow_unit_of("e_import") == ("W", "W")
        assert flow_unit_of("e_battery") == ("J", "J")
        assert flow_unit_of("soc") == ("-", "1")
        assert flow_unit_of("pv_gen") == ("W", "W")

    def test_flow_unit_fallback(self):
        assert flow_unit_of("p_pump") == ("W", "W")  # 功率前缀
        assert flow_unit_of("h_boiler") == ("W", "W")
        assert flow_unit_of("e_bat") == ("J", "J")  # 能量前缀
        assert flow_unit_of("u_ch") == ("-", "1")  # 0/1 控制量
        assert flow_unit_of("foo") == ("-", "1")  # 未知名兜底

    def test_hourly_meta(self):
        meta = hourly_meta(["e_import", "e_battery", "soc", "pv_gen"])
        assert meta == {
            "units": {
                "e_import": {"unit": "W", "si": "W"},
                "e_battery": {"unit": "J", "si": "J"},
                "soc": {"unit": "-", "si": "1"},
                "pv_gen": {"unit": "W", "si": "W"},
            }
        }
