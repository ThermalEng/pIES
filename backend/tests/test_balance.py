"""三母线平衡矩阵构建器单元测试(02 §3):守恒系数行正确性手算验证。

纯计算测试,不依赖 DB。
"""

import numpy as np
import pytest
from scipy import sparse

from iesplan.engines.balance import (
    build_cold_balance,
    build_electric_balance,
    build_grid_capacity,
    build_heat_balance,
    build_pump_equation,
)

N = 4  # 测试步数


def _layout(blocks: list[str]) -> dict[str, int]:
    """按块顺序构造布局:每块 N 个连续列。"""
    out: dict[str, int] = {}
    start = 0
    for name in blocks:
        out[name] = start
        start += N
    return out


class TestElectricBalance:
    """电平衡 E-BAL(02 §3.2):购电+光伏+放电 = 负荷+售电+充电+热泵+制冷机+泵。"""

    def test_coefficient_row(self):
        lay = _layout([
            "p_grid_buy", "p_grid_sell", "p_pv", "p_bat_ch", "p_bat_dis",
            "p_hp_elec", "p_chiller_elec", "p_pump",
        ])
        e_load = np.array([100.0, 200.0, 300.0, 400.0])
        con = build_electric_balance(
            N, 8 * N, e_load=e_load,
            p_grid_buy=lay["p_grid_buy"], p_grid_sell=lay["p_grid_sell"],
            p_pv=lay["p_pv"], p_bat_ch=lay["p_bat_ch"], p_bat_dis=lay["p_bat_dis"],
            p_hp_elec=lay["p_hp_elec"], p_chiller_elec=lay["p_chiller_elec"],
            p_pump=lay["p_pump"],
        )
        A = con.A
        assert A.shape == (N, 8 * N)
        assert sparse.issparse(A)  # 稀疏表示(全年 8760 步内存可控,02 §11.1)
        # 第 τ 行:供给系数 +1(购电/光伏/放电),需求系数 -1(售电/充电/热泵/制冷机/泵)
        for tau in range(N):
            row = A[tau].toarray().ravel()  # 稀疏矩阵行 → 稠密向量
            assert row[lay["p_grid_buy"] + tau] == pytest.approx(1.0)
            assert row[lay["p_pv"] + tau] == pytest.approx(1.0)
            assert row[lay["p_bat_dis"] + tau] == pytest.approx(1.0)
            assert row[lay["p_grid_sell"] + tau] == pytest.approx(-1.0)
            assert row[lay["p_bat_ch"] + tau] == pytest.approx(-1.0)
            assert row[lay["p_hp_elec"] + tau] == pytest.approx(-1.0)
            assert row[lay["p_chiller_elec"] + tau] == pytest.approx(-1.0)
            assert row[lay["p_pump"] + tau] == pytest.approx(-1.0)
            # 其余列必须为 0
            mask = np.ones(8 * N, dtype=bool)
            mask[[lay["p_grid_buy"] + tau, lay["p_pv"] + tau, lay["p_bat_dis"] + tau,
                  lay["p_grid_sell"] + tau, lay["p_bat_ch"] + tau, lay["p_hp_elec"] + tau,
                  lay["p_chiller_elec"] + tau, lay["p_pump"] + tau]] = False
            assert np.all(row[mask] == 0.0)
        # 等式约束:lb == ub == 负荷
        assert np.all(con.lb == con.ub)
        assert np.allclose(con.lb, e_load)

    def test_balance_equation_holds_for_consistent_flows(self):
        lay = _layout(["p_grid_buy", "p_grid_sell", "p_pv", "p_bat_ch", "p_bat_dis",
                       "p_hp_elec", "p_chiller_elec", "p_pump"])
        e_load = np.array([1.0, 2.0, 3.0, 4.0])
        con = build_electric_balance(
            N, 8 * N, e_load=e_load,
            p_grid_buy=lay["p_grid_buy"], p_grid_sell=lay["p_grid_sell"],
            p_pv=lay["p_pv"], p_bat_ch=lay["p_bat_ch"], p_bat_dis=lay["p_bat_dis"],
            p_hp_elec=lay["p_hp_elec"], p_chiller_elec=lay["p_chiller_elec"],
            p_pump=lay["p_pump"],
        )
        # 手算一组守恒流:buy = 负荷 + 充电 + hp + chl + pump - pv - dis
        x = np.zeros(8 * N)
        pv = np.array([5.0, 0.0, 0.0, 0.0])
        dis = np.array([0.0, 0.0, 1.0, 0.0])
        ch = np.array([0.0, 1.0, 0.0, 0.0])
        hp = np.array([0.5, 0.5, 0.5, 0.5])
        chl = np.array([0.2, 0.2, 0.2, 0.2])
        pump = np.array([0.1, 0.1, 0.1, 0.1])
        buy = e_load + ch + hp + chl + pump - pv - dis
        x[lay["p_grid_buy"]: lay["p_grid_buy"] + N] = buy
        x[lay["p_pv"]: lay["p_pv"] + N] = pv
        x[lay["p_bat_dis"]: lay["p_bat_dis"] + N] = dis
        x[lay["p_bat_ch"]: lay["p_bat_ch"] + N] = ch
        x[lay["p_hp_elec"]: lay["p_hp_elec"] + N] = hp
        x[lay["p_chiller_elec"]: lay["p_chiller_elec"] + N] = chl
        x[lay["p_pump"]: lay["p_pump"] + N] = pump
        residual = con.A @ x - con.ub
        assert np.allclose(residual, 0.0, atol=1e-12)


class TestHeatColdBalance:
    """热/冷平衡 H-BAL / C-BAL(02 §3.3/§3.4):输配损耗供应侧系数 (1+λ)。"""

    def test_heat_balance_with_loss(self):
        lay = _layout(["p_boiler", "p_hp_heat", "p_shed_h"])
        h_load = np.array([100.0, 200.0, 0.0, 50.0])
        con = build_heat_balance(
            N, 3 * N, h_load=h_load,
            p_boiler=lay["p_boiler"], p_hp_heat=lay["p_hp_heat"],
            p_shed_h=lay["p_shed_h"], lambda_h=0.05,
        )
        A = con.A
        assert np.allclose(con.lb, 1.05 * h_load)
        for tau in range(N):
            assert A[tau, lay["p_boiler"] + tau] == pytest.approx(1.0)
            assert A[tau, lay["p_hp_heat"] + tau] == pytest.approx(1.0)
            assert A[tau, lay["p_shed_h"] + tau] == pytest.approx(1.05)  # 削减项系数 (1+λ)

    def test_cold_balance_with_loss(self):
        lay = _layout(["p_chiller", "p_hp_cool"])
        c_load = np.array([10.0, 20.0, 30.0, 40.0])
        con = build_cold_balance(
            N, 2 * N, c_load=c_load,
            p_chiller=lay["p_chiller"], p_hp_cool=lay["p_hp_cool"], lambda_c=0.08,
        )
        assert np.allclose(con.lb, 1.08 * c_load)
        A = con.A
        for tau in range(N):
            assert A[tau, lay["p_chiller"] + tau] == pytest.approx(1.0)
            assert A[tau, lay["p_hp_cool"] + tau] == pytest.approx(1.0)

    def test_negative_loss_rejected(self):
        with pytest.raises(ValueError):
            build_heat_balance(N, 2 * N, h_load=np.zeros(N),
                               p_boiler=0, p_hp_heat=N, lambda_h=-0.1)


class TestPumpEquation:
    """泵耗电方程 PUMP(02 §3.5):p_pump = c_ph·Q_h + c_pc·Q_c。"""

    def test_coefficients(self):
        lay = _layout(["p_pump", "p_boiler", "p_hp_heat", "p_chiller", "p_hp_cool"])
        con = build_pump_equation(
            N, 5 * N,
            p_pump=lay["p_pump"], p_boiler=lay["p_boiler"], p_hp_heat=lay["p_hp_heat"],
            p_chiller=lay["p_chiller"], p_hp_cool=lay["p_hp_cool"],
            c_ph=0.02, c_pc=0.02,
        )
        A = con.A
        for tau in range(N):
            assert A[tau, lay["p_pump"] + tau] == pytest.approx(1.0)
            assert A[tau, lay["p_boiler"] + tau] == pytest.approx(-0.02)
            assert A[tau, lay["p_hp_heat"] + tau] == pytest.approx(-0.02)
            assert A[tau, lay["p_chiller"] + tau] == pytest.approx(-0.02)
            assert A[tau, lay["p_hp_cool"] + tau] == pytest.approx(-0.02)
        assert np.all(con.lb == 0.0) and np.all(con.ub == 0.0)


class TestGridCapacity:
    """并网约束 GRID-CAP(02 §3.6):容量上限 + 禁止反送电等式。"""

    def test_capacity_limits(self):
        lay = _layout(["p_grid_buy", "p_grid_sell"])
        cons = build_grid_capacity(N, 2 * N, p_grid_buy=lay["p_grid_buy"],
                                   p_grid_sell=lay["p_grid_sell"],
                                   c_import=5000.0, c_export=2000.0, forbid_reverse_feed=False)
        assert len(cons) == 2
        assert np.all(cons[0].ub == 5000.0)  # 购电 ≤ C_imp
        assert np.all(cons[1].ub == 2000.0)  # 售电 ≤ C_exp
        assert np.all(cons[0].lb == -np.inf)

    def test_forbid_reverse_feed_equality(self):
        lay = _layout(["p_grid_buy", "p_grid_sell"])
        cons = build_grid_capacity(N, 2 * N, p_grid_buy=lay["p_grid_buy"],
                                   p_grid_sell=lay["p_grid_sell"],
                                   c_import=5000.0, c_export=2000.0, forbid_reverse_feed=True)
        assert len(cons) == 3
        # 第三条:售电 = 0 等式约束
        eq = cons[2]
        assert np.all(eq.lb == 0.0) and np.all(eq.ub == 0.0)
        for tau in range(N):
            assert eq.A[tau, lay["p_grid_sell"] + tau] == pytest.approx(1.0)
