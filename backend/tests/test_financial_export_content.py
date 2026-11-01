"""财务摘要下载与封存事实的精确对照(测试断言, 非运行期校验)。

阳性: 检入的真实下载 xlsx(财务摘要表)与同一封存 JSON 逐行
(标签、值、顺序、行数)精确一致, 覆盖 K/N/E/C/币种、付款时点
比例金额(同一 t 多笔按序)、每年度全部分量、CF 每个时点净额/
贴现/累计/贴现累计、完整税后现金流数组、逐月 VAT 实缴/确认/
到账/结转、退款计划(期后到账标应收)、所得税/折旧/亏损余额,
以及 NPV 与 IRR/回收期值和状态。

检入封存块为历史 ``ies.financial-evaluation@1.0.0``(tax_cash 与
annual_net_flows 必填): 缺必填字段按契约违例暴露, 不静默补空
税务表。

阴性对照(不入生产): 内存中交换两 CF 行数值 / 错改 K(相同数字
仍在别处存在), 精确对照必须失败——旧全表子串命中对此类篡改
仍为绿, 见提交说明中的对照证据。
"""

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from xlsx_content_check import assert_block_exact, extract_pairs

FIXTURES = Path(__file__).resolve().parent / "fixtures_t3_export"


def _load_fixtures():
    sealed = json.loads((FIXTURES / "t3-sealed.json").read_text(encoding="utf-8"))
    wb = load_workbook(FIXTURES / "t3-report.xlsx", read_only=True, data_only=True)
    return sealed, extract_pairs(wb["财务摘要"])


def test_downloaded_xlsx_matches_sealed_facts_exactly():
    sealed, pairs = _load_fixtures()
    assert sealed["schema_version"] == "1.0.0"  # 历史检入封存样例，不能冒充新版数据
    assert sealed["net_cashflows"] == []
    assert all("base_from" in row for row in sealed["tax_cash"]["depreciation"])
    assert_block_exact(pairs, sealed)


def test_negative_swapped_cf_nets_must_fail():
    """交换 CF[t=0]/CF[t=1] 净额: 数字仍在表内, 精确对照必须失败。"""
    sealed, pairs = _load_fixtures()
    net_rows = [i for i, (label, _) in enumerate(pairs) if label.endswith("净额 / Net")]
    assert len(net_rows) >= 2
    a, b = net_rows[0], net_rows[1]
    pairs[a], pairs[b] = (pairs[a][0], pairs[b][1]), (pairs[b][0], pairs[a][1])
    with pytest.raises(AssertionError, match="值不符"):
        assert_block_exact(pairs, sealed)


def _tax_seal() -> dict:
    """合成含税封存(E=1/K=0)：12 月单池、期后到账退款、所得税/折旧/余额各一。"""
    vat = [
        {
            "pool": "main_pool",
            "month": month,
            "paid": "10.5" if month == 3 else "0.0",
            "refund_claimed": "5.0" if month == 3 else "0.0",
            "refund_arrived": "5.0" if month == 5 else "0.0",
            "carry": "100.25",
        }
        for month in range(1, 13)
    ]
    return {
        "currency": "CNY",
        "evaluation_years": 1,
        "annual_net_flows": ["-1000.0", "2500.5"],
        "net_cashflows": [{"id": "recovery", "t": 1, "direction": "inflow", "amount": "37.25"}],
        "tax_cash": {
            "vat": vat,
            "refunds": [
                {"pool": "main_pool", "period": 3, "arrival_period": 5, "amount": "5.0"},
                {"pool": "main_pool", "period": 11, "arrival_period": 14, "amount": "7.5"},
            ],
            "income": [
                {
                    "rule": "income",
                    "year": 1,
                    "taxable_base": "2000.0",
                    "loss_used": "500.0",
                    "loss_generated": "0.0",
                    "loss_expired": "0.0",
                    "tax": "375.0",
                }
            ],
            "depreciation": [{"rule": "depr", "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system"}, "year": 1, "amount": "800.0"}],
            "loss_balances": [
                {
                    "rule": "income",
                    "vintage_id": "vintage-with-a-very-long-identifier-0001",
                    "year": 1,
                    "last_usable_year": 6,
                    "amount": "1200.0",
                }
            ],
        },
        "evaluation": {
            "rows": [
                {
                    "year": 0, "net": "-1000.0", "discounted": "-1000.0",
                    "cumulative": "-1000.0", "cumulative_discounted": "-1000.0",
                },
                {
                    "year": 1, "net": "2500.5", "discounted": "2314.0",
                    "cumulative": "1500.5", "cumulative_discounted": "1314.0",
                },
            ],
            "npv": "1314.0",
            "npv_unit": "CNY",
            "irr": "1.5",
            "irr_status": "normal",
            "static_payback": 1,
            "static_payback_unit": "year",
            "static_status": "normal",
            "dynamic_payback": 1,
            "dynamic_payback_unit": "year",
            "dynamic_status": "normal",
        },
    }


def test_tax_rows_consume_sealed_facts_without_recompute():
    """税务明细行消费封存原样：金额全出现、无截断、期后到账标应收。"""
    from iesplan.application.packages.transfers import _sealed_tax_rows

    fin = _tax_seal()
    rows = _sealed_tax_rows(fin, "CNY")
    values = [value for _, value in rows]
    for amount in (
        "37.25", "-1000.0", "2500.5", "10.5", "5.0", "100.25", "7.5",
        "2000.0", "500.0", "375.0", "800.0", "1200.0",
    ):
        assert any(amount in str(value) for value in values), amount
    # 长 vintage 不截断。
    assert any("vintage-with-a-very-long-identifier-0001" in label for label, _ in rows)
    # 期后到账标应收，期内到账不标。
    post = [label for label, _ in rows if "确认m=11→到账m=14" in label]
    assert len(post) == 1 and "(期后应收，非期内现金)" in post[0]
    within = [label for label, _ in rows if "确认m=3→到账m=5" in label]
    assert len(within) == 1 and "期后应收" not in within[0]
    # 行数精确：显式净现金 1 + CF 2 + VAT 48 + 退款 2 + 所得税 5 + 折旧 1 + 余额 1。
    assert rows[0][0].startswith("显式净现金 recovery t=1 inflow")
    assert len(rows) == 1 + 2 + 48 + 2 + 5 + 1 + 1


def test_tax_expected_mirror_matches_production():
    """测试镜像与生产渲染逐行一致（供未来检入下载精确对照）。"""
    from xlsx_content_check import _expected_tax_pairs

    from iesplan.application.packages.transfers import _excel_safe, _sealed_tax_rows

    fin = _tax_seal()
    assert _expected_tax_pairs(fin, "CNY") == [
        (label, _excel_safe(value)) for label, value in _sealed_tax_rows(fin, "CNY")
    ]


def test_tax_rows_absent_for_missing_required_fields():
    """封存块缺必填 tax_cash/annual_net_flows：按契约违例暴露，不静默补空税务。"""
    from iesplan.application.packages.transfers import _sealed_tax_rows

    with pytest.raises(KeyError):
        _sealed_tax_rows({"currency": "CNY", "evaluation_years": 1}, "CNY")
    missing_net = _tax_seal()
    del missing_net["net_cashflows"]
    with pytest.raises(KeyError, match="net_cashflows"):
        _sealed_tax_rows(missing_net, "CNY")


def test_negative_wrong_k_must_fail():
    """错改 K=1→2(数字 2 在 N/年度行仍存在): 精确对照必须失败。"""
    sealed, pairs = _load_fixtures()
    idx = next(
        i for i, (label, _) in enumerate(pairs) if label.startswith("建设期 K")
    )
    pairs[idx] = (pairs[idx][0], 2)
    with pytest.raises(AssertionError, match="值不符"):
        assert_block_exact(pairs, sealed)
