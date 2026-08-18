"""环境指标单元测试:运行期排放核算(02 §8 与附录 B 默认因子)。

纯计算测试,不依赖 DB。
"""

import numpy as np
import pytest

from iesplan.metrics.environmental import operational_emissions


class TestOperationalEmissions:
    """排放核算:手算验证、逐时序列、边界与因子版本绑定。"""

    def test_hand_calculation(self):
        # 电网 1000 kWh × 0.581 = 581;燃气 500 m3 × 2.0 = 1000;合计 1581
        result = operational_emissions(
            {"grid_purchase": 1000, "gas": 500},
            {"grid_purchase": 0.581, "gas": 2.0},
            boundary="scope1+scope2",
            factor_version="2024-v1.0",
            data_refs=["ds-1", "factor-src-42"],
        )
        assert result["total_kg"] == pytest.approx(1581.0)
        assert result["by_fuel"]["grid_purchase"]["emissions_kg"] == pytest.approx(581.0)
        assert result["by_fuel"]["gas"]["emissions_kg"] == pytest.approx(1000.0)
        assert result["by_fuel"]["gas"]["unit"] == "m3"
        assert result["by_fuel"]["grid_purchase"]["unit"] == "kWh"

    def test_boundary_and_factor_version_bound(self):
        result = operational_emissions(
            {"grid_purchase": 100},
            {"grid_purchase": 0.5},
            boundary="full_lifecycle",
            factor_version="2026-08",
        )
        # REQ-ENV-001:边界与因子版本必须随输出绑定
        assert result["boundary"] == "full_lifecycle"
        assert result["factor_version"] == "2026-08"
        assert result["data_refs"] == []
        assert result["definition_version"] == "1.0.0"

    def test_hourly_series_summed(self):
        # 逐时 1 kW × 8760 h = 8760 kWh × 0.581
        series = np.ones(8760)
        result = operational_emissions(
            {"grid_purchase": series},
            {"grid_purchase": 0.581},
            boundary="scope2",
            factor_version="v1",
        )
        assert result["total_kg"] == pytest.approx(8760 * 0.581, abs=0.5)

    def test_missing_factor_excluded_and_listed(self):
        result = operational_emissions(
            {"grid_purchase": 1000, "gas": 500},
            {"grid_purchase": 0.581},  # gas 无因子
            boundary="scope1+scope2",
            factor_version="v1",
        )
        assert result["total_kg"] == pytest.approx(581.0)
        assert "gas" not in result["by_fuel"]
        assert result["missing_factors"] == ["gas"]

    def test_empty_flows(self):
        result = operational_emissions({}, {}, boundary="scope1", factor_version="v1")
        assert result["total_kg"] == 0.0
        assert result["by_fuel"] == {}

    def test_decimal_input(self):
        from decimal import Decimal

        result = operational_emissions(
            {"grid_purchase": Decimal("1000")},
            {"grid_purchase": Decimal("0.581")},
            boundary="scope2",
            factor_version="v1",
        )
        assert result["total_kg"] == pytest.approx(581.0)

    def test_missing_boundary_rejected(self):
        with pytest.raises(ValueError):
            operational_emissions({"a": 1}, {"a": 1}, boundary="", factor_version="v1")
        with pytest.raises(ValueError):
            operational_emissions({"a": 1}, {"a": 1}, boundary="scope1", factor_version="")
