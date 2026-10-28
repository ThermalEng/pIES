"""财务摘要 xlsx 与封存事实的精确对照(测试断言, 非运行期校验)。

逐单元格比较标签+对应值(含顺序、重复标签按位置对应), 替代
全表子串命中: 行对调、错改 K 等只要相同数字仍在别处存在,
子串检查就会漏过, 本检查必失败。

期望行按 :func:`_expected_pairs` 从封存字典独立组装展示文本
(只组装、不重算财务业务); 单元格转义复用生产 ``_excel_safe``
(M-09 防公式注入, 同源展示规则)。
"""

from iesplan.application.packages.transfers import _excel_safe

BLOCK_START_LABEL = "评价年限 E / Evaluation years"


def extract_pairs(ws) -> list:
    """从 row=3 起按序读出 (A列标签, B列值), 遇空标签停止。"""
    pairs = []
    row = 3
    while True:
        label = ws.cell(row=row, column=1).value
        if label is None:
            break
        pairs.append((label, ws.cell(row=row, column=2).value))
        row += 1
    return pairs


def _expected_pairs(fin: dict) -> list:
    """从封存事实组装期望 (标签, 值) 序列, 与生产展示文本逐项对应。"""
    evaluation = fin["evaluation"]
    ccy = fin["currency"]
    rows = [
        ("评价年限 E / Evaluation years", fin["evaluation_years"]),
        ("建设期 K(财务时点 t=0..K) / Construction period", fin["construction_period"]),
        ("运营年限 N(第 j 年落 t=K+j) / Operation years", fin["operation_years"]),
        ("币种 / Currency", ccy),
        ("价格口径 / Price basis", fin["price_basis"]),
        ("基年 / Base year", fin["base_year"]),
        ("折现率(冻结) / Discount rate", fin["discount_rate"]),
        (
            "建设总额 C / Construction total",
            f"{fin['construction_total']} {fin['construction_total_unit']}",
        ),
    ]
    # 同一 t 多笔付款允许: 按封存列表顺序逐行对应, 不用 dict 覆盖。
    for item in fin["payments_applied"]:
        rows.append(
            (
                f"建设付款 t={item['t']} / Payment t={item['t']}",
                f"{item['ratio']} × C = {item['amount']} {ccy}",
            )
        )
    for index, item in enumerate(fin["annual_operation"]):
        rows.append(
            (
                f"运营年第 {index + 1} 年(t={item['financial_year']}) / Year {index + 1}",
                f"能源 {item['energy_net']} − 可变 {item['variable_om']} "
                f"− 固定 {item['fixed_om']} − 维护 {item['maintenance']} "
                f"= 净额 {item['net']} {ccy}",
            )
        )
    for item in evaluation["rows"]:
        rows.append(
            (
                f"CF[t={item['year']}] 净额 / Net",
                f"{item['net']} {ccy}",
            )
        )
        rows.append(
            (
                f"CF[t={item['year']}] 贴现/累计/贴现累计",
                f"{item['discounted']} / {item['cumulative']} / "
                f"{item['cumulative_discounted']} {ccy}",
            )
        )
    rows.extend(_expected_tax_pairs(fin, ccy))
    rows.append(("净现值 NPV / NPV", f"{evaluation['npv']} {evaluation['npv_unit']}"))
    irr = evaluation["irr"]
    rows.append(
        (
            "内部收益率 IRR / IRR",
            f"{irr} / {evaluation['irr_status']}"
            if irr is not None
            else f"— / {evaluation['irr_status']}",
        )
    )
    for key, label in (("static", "静态回收期"), ("dynamic", "动态回收期")):
        value = evaluation[f"{key}_payback"]
        if value is not None:
            rows.append(
                (
                    f"{label} / {key} payback",
                    f"{value} {evaluation[f'{key}_payback_unit']} / "
                    f"{evaluation[f'{key}_status']}",
                )
            )
        else:
            rows.append((f"{label} / {key} payback", f"— / {evaluation[f'{key}_status']}"))
    return [(label, _excel_safe(value)) for label, value in rows]


def _expected_tax_pairs(fin: dict, ccy: str) -> list:
    """税务明细期望行(独立组装展示文本，只组装不重算财务业务)。

    与生产 ``_sealed_tax_rows`` 逐行对应：封存块 3.0.0 的 ``tax_cash`` 与
    ``annual_net_flows`` 必填，直接消费；合法空数组显式记“无适用规则”。
    """
    tax = fin["tax_cash"]
    horizon = int(fin["evaluation_years"]) * 12
    rows = []
    cashflows = fin["net_cashflows"]
    for item in cashflows:
        rows.append((
            f"显式净现金 {item['id']} t={item['t']} {item['direction']} / Explicit net cashflow",
            f"{item['amount']} {ccy}",
        ))
    if not cashflows:
        rows.append(("显式净现金 / Explicit net cashflows", "无项目 / None"))
    for index, amount in enumerate(fin["annual_net_flows"]):
        rows.append((f"税后现金流 CF[t={index}] / After-tax CF[t={index}]", f"{amount} {ccy}"))
    vat = tax["vat"]
    if vat:
        for row in vat:
            head = f"增值税 {row['pool']} m={row['month']} / VAT {row['pool']} m={row['month']}"
            rows.append((f"{head} 实缴 / paid", f"{row['paid']} {ccy}"))
            rows.append((f"{head} 确认退款 / claimed", f"{row['refund_claimed']} {ccy}"))
            rows.append((f"{head} 到账退款 / arrived", f"{row['refund_arrived']} {ccy}"))
            rows.append((f"{head} 期末结转 / carry", f"{row['carry']} {ccy}"))
    else:
        rows.append(("增值税 / VAT", "无适用规则 / No applicable rules"))
    refunds = tax["refunds"]
    if refunds:
        for row in refunds:
            post = "(期后应收，非期内现金)" if int(row["arrival_period"]) > horizon else ""
            rows.append(
                (
                    f"退款 {row['pool']} 确认m={row['period']}→到账m={row['arrival_period']}{post} "
                    f"/ Refund {row['pool']} confirm m={row['period']}→arrive m={row['arrival_period']}",
                    f"{row['amount']} {ccy}",
                )
            )
    else:
        rows.append(("退款计划 / Refunds", "无适用规则 / No applicable rules"))
    income = tax["income"]
    if income:
        for row in income:
            head = f"所得税 {row['rule']} y={row['year']} / Income {row['rule']} y={row['year']}"
            rows.append((f"{head} 税基 / base", f"{row['taxable_base']} {ccy}"))
            rows.append((f"{head} 抵亏 / used", f"{row['loss_used']} {ccy}"))
            rows.append((f"{head} 新生亏损 / generated", f"{row['loss_generated']} {ccy}"))
            rows.append((f"{head} 到期核销 / expired", f"{row['loss_expired']} {ccy}"))
            rows.append((f"{head} 税额 / tax", f"{row['tax']} {ccy}"))
    else:
        rows.append(("所得税 / Income tax", "无适用规则 / No applicable rules"))
    depreciation = tax["depreciation"]
    if depreciation:
        for row in depreciation:
            source = row["base_from"]
            identity = (f"{source['kind']}:{source['finance_type']}"
                        if source["kind"] == "upfront_capex_net"
                        else f"{source['kind']}:y={source['year']},#{source['position']}")
            rows.append(
                (
                    f"计税折旧 {row['rule']} {identity} y={row['year']} / "
                    f"Depreciation {row['rule']} {identity} y={row['year']}",
                    f"{row['amount']} {ccy}",
                )
            )
    else:
        rows.append(("计税折旧 / Depreciation", "无适用规则 / No applicable rules"))
    balances = tax["loss_balances"]
    if balances:
        for row in balances:
            rows.append(
                (
                    f"亏损余额 {row['rule']} {row['vintage_id']} y={row['year']} "
                    f"(可用至y={row['last_usable_year']}) / Loss balance",
                    f"{row['amount']} {ccy}",
                )
            )
    else:
        rows.append(("亏损余额 / Loss balances", "无适用规则 / No applicable rules"))
    return [(label, _excel_safe(value)) for label, value in rows]


def assert_block_exact(pairs: list, sealed: dict) -> None:
    """断言财务摘要块与封存事实逐行精确一致(标签、值、顺序、行数)。

    ``pairs`` 为工作表按下发的行顺序读出的 (标签, 值)。
    不一致即 ``AssertionError``, 指出首个差异行。
    """
    expected = _expected_pairs(sealed)
    assert pairs, "财务摘要表 row=3 起无数据"
    assert pairs[0][0] == BLOCK_START_LABEL, f"块起始标签不符: {pairs[0][0]!r}"
    assert len(pairs) == len(expected), (
        f"块行数不符: xlsx {len(pairs)} 行, 封存期望 {len(expected)} 行"
    )
    for index, ((label, value), (exp_label, exp_value)) in enumerate(
        zip(pairs, expected, strict=True)
    ):
        row_no = index + 3
        assert label == exp_label, f"row={row_no} 标签不符: {label!r} != {exp_label!r}"
        assert value == exp_value, (
            f"row={row_no}({label}) 值不符: {value!r} != {exp_value!r}"
        )
