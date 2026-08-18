"""单位模块单元测试:换算正确性、跨类拒绝、仿射温度、中英展示格式。"""

import pytest

from iesplan.core.units import (
    UnitError,
    convert,
    energy_to_joules,
    format_value,
    power_to_watts,
    temperature_kelvin,
)


class TestConvertEnergy:
    """能量换算(02 §2.2:1 kWh = 3.6e6 J)。"""

    @pytest.mark.parametrize(
        "value, from_unit, to_unit, expected",
        [
            (1.0, "kWh", "J", 3.6e6),
            (1.0, "MWh", "J", 3.6e9),
            (1.0, "GJ", "J", 1e9),
            (1.0, "J", "J", 1.0),
            (2.5, "kWh", "MWh", 2.5 / 1000.0),
            (1000.0, "kWh", "MWh", 1.0),
            (1.0, "kcal", "J", 4186.8),
            (3.6e6, "J", "kWh", 1.0),
            # 别名(04 §8.2:"度" = kWh)
            (1.0, "度", "J", 3.6e6),
        ],
    )
    def test_convert(self, value, from_unit, to_unit, expected):
        assert convert(value, from_unit, to_unit) == pytest.approx(expected)

    def test_energy_to_joules(self):
        assert energy_to_joules(1.0, "kWh") == pytest.approx(3.6e6)
        assert energy_to_joules(1.0, "MWh") == pytest.approx(3.6e9)
        assert energy_to_joules(1.0, "GJ") == pytest.approx(1e9)
        assert energy_to_joules(1.0, "J") == 1.0


class TestConvertPower:
    """功率换算(02 §2.2:1 kW = 1e3 W)。"""

    def test_power_to_watts(self):
        assert power_to_watts(1.0, "kW") == pytest.approx(1000.0)
        assert power_to_watts(1.0, "MW") == pytest.approx(1e6)
        assert power_to_watts(1.0, "W") == 1.0
        assert power_to_watts(2.5, "MW") == pytest.approx(2.5e6)

    def test_convert_between_power_units(self):
        assert convert(1000.0, "kW", "MW") == pytest.approx(1.0)
        assert convert(1.0, "MW", "kW") == pytest.approx(1000.0)
        assert convert(1.0, "GW", "MW") == pytest.approx(1000.0)


class TestConvertTemperature:
    """温度仿射换算(02 §2.2:T[K] = θ[°C] + 273.15)。"""

    def test_temperature_kelvin(self):
        assert temperature_kelvin(0.0, "C") == pytest.approx(273.15)
        assert temperature_kelvin(25.0, "C") == pytest.approx(298.15)
        assert temperature_kelvin(273.15, "K") == pytest.approx(273.15)

    def test_kelvin_to_celsius(self):
        assert convert(273.15, "K", "C") == pytest.approx(0.0)
        assert convert(298.15, "K", "C") == pytest.approx(25.0)
        assert convert(0.0, "C", "F") == pytest.approx(32.0)
        assert convert(100.0, "C", "K") == pytest.approx(373.15)


class TestConvertOther:
    """时长/角度/金额换算。"""

    def test_duration(self):
        assert convert(1.0, "h", "s") == 3600.0
        assert convert(1.0, "min", "s") == 60.0
        assert convert(1.0, "a", "h") == pytest.approx(8760.0)

    def test_angle(self):
        assert convert(180.0, "deg", "rad") == pytest.approx(3.141592653589793)
        assert convert(3.141592653589793, "rad", "deg") == pytest.approx(180.0)

    def test_currency_same_unit(self):
        assert convert(1.0, "CNY", "CNY") == 1.0


class TestConvertErrors:
    """拒绝非法输入。"""

    def test_cross_category_rejected(self):
        with pytest.raises(UnitError):
            convert(1.0, "kWh", "kW")

    def test_unknown_unit_rejected(self):
        with pytest.raises(UnitError):
            convert(1.0, "not_a_unit", "J")

    def test_case_insensitive(self):
        assert convert(1.0, "KWH", "J") == pytest.approx(3.6e6)


class TestFormatValue:
    """中英展示格式(仅展示,不用于计算)。"""

    def test_zh(self):
        assert format_value(3.6, "kWh", "zh") == "3.6 千瓦时"
        assert format_value(1.0, "MW", "zh") == "1 兆瓦"

    def test_en(self):
        assert format_value(3.6, "kWh", "en") == "3.6 kWh"
        assert format_value(25.0, "C", "en") == "25 °C"

    def test_format_from_decimal_str(self):
        # 金额类(展示层用 Decimal)
        assert format_value("12.34", "CNY", "zh") == "12.34 元"
