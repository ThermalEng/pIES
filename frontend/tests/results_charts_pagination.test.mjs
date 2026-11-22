/**
 * 定向单测：结果页图表原语、纯净数据映射、URL 导航与分页逻辑。
 *
 * 覆盖：
 * 1. parseChartNumber / computeAxisRange / pickTickIndices 的边界与零伪造语义；
 * 2. parseResultNavigation / buildResultSearchParams 与 paginateItems 的纯导航/分页逻辑；
 * 3. buildCashflowChartData / buildAnnualOperationChartData / buildMonthlyDemandChartData /
 *    buildVatPoolChartData / buildIncomeTaxChartData / buildDepreciationChartData /
 *    buildComparisonChartData / buildAssetBookValueChartData / buildNamedAmountChartGroups /
 *    buildCostItemChartGroups（绝不重算、精确保留定点小数原文、支持多实体切换与预览截断提示）；
 * 4. BarChart / ComboChart / CategoryLineChart / LineChart(toggleable) / Tabs / Pagination /
 *    分页模式 FinancialRelations / FinancialTransactions / FinancialComparison 的 SSR 渲染。
 */

import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)

const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const { I18nProvider } = await import('../src/i18n/index.ts')
const { Tabs, Pagination } = await import('../src/components/ui.tsx')
const {
  BarChart,
  CategoryLineChart,
  ComboChart,
  LineChart,
  parseChartNumber,
  computeAxisRange,
  pickTickIndices,
} = await import('../src/components/charts.tsx')
const {
  parseResultNavigation,
  buildResultSearchParams,
  paginateItems,
  buildCashflowChartData,
  buildAnnualOperationChartData,
  buildMonthlyDemandChartData,
  buildVatPoolChartData,
  buildIncomeTaxChartData,
  buildDepreciationChartData,
  buildComparisonChartData,
  buildAssetBookValueChartData,
  buildNamedAmountChartGroups,
  buildCostItemChartGroups,
  FinancialRelations,
  FinancialTransactions,
  FinancialComparison,
} = await import('../src/features/results/index.ts')

// 1. 纯图表工具断言
assert.equal(parseChartNumber('123.45000000000000000001'), 123.45)
assert.equal(parseChartNumber('-0.0001'), -0.0001)
assert.equal(parseChartNumber(0), 0)
assert.equal(parseChartNumber(''), null)
assert.equal(parseChartNumber('not-a-number'), null)
assert.equal(parseChartNumber(null), null)
assert.equal(parseChartNumber(NaN), null)

{
  const emptyRange = computeAxisRange([null, null], true)
  assert.deepEqual(emptyRange, { yMin: 0, yMax: 1, ticks: [0, 0.25, 0.5, 0.75, 1] })

  const mixedRange = computeAxisRange([-100, 50, 200], true)
  assert.ok(mixedRange.yMin < -100 && mixedRange.yMax > 200)
  assert.equal(mixedRange.ticks.length, 5)

  const positiveOnly = computeAxisRange([10, 50], true)
  assert.equal(positiveOnly.yMin, 0)
  assert.ok(positiveOnly.yMax > 50)
}

assert.deepEqual(pickTickIndices(0, 8), [])
assert.deepEqual(pickTickIndices(4, 8), [0, 1, 2, 3])
{
  const dense = pickTickIndices(240, 8)
  assert.equal(dense[0], 0)
  assert.equal(dense[dense.length - 1], 239)
  assert.equal(dense.length, 8)
}

// 2. URL 导航解析与构建（绝不遗留 package=）
assert.deepEqual(parseResultNavigation(new URLSearchParams('')), { tab: 'overview', detail: null })
assert.deepEqual(parseResultNavigation(new URLSearchParams('?tab=tax')), { tab: 'tax', detail: null })
assert.deepEqual(parseResultNavigation(new URLSearchParams('?tab=overview&detail=demand')), {
  tab: 'tax',
  detail: 'demand',
})
assert.deepEqual(parseResultNavigation(new URLSearchParams('?tab=unknown&detail=bogus')), {
  tab: 'overview',
  detail: null,
})

{
  const fromPkg = new URLSearchParams('?package=42&tab=financial')
  const built = buildResultSearchParams(fromPkg, { tab: 'tax', detail: 'vat' })
  assert.equal(built.get('package'), null)
  assert.equal(built.get('tab'), 'tax')
  assert.equal(built.get('detail'), 'vat')

  const cleared = buildResultSearchParams(built, { tab: 'overview', detail: null })
  assert.equal(cleared.toString(), '')
}

// 3. 分页切片与页码钳位
{
  const rows = Array.from({ length: 53 }, (_, i) => i + 1)
  const p1 = paginateItems(rows, 1, 20)
  assert.equal(p1.totalPages, 3)
  assert.equal(p1.items.length, 20)
  assert.equal(p1.items[0], 1)
  assert.equal(p1.items[19], 20)

  const clamped = paginateItems(rows, 99, 20)
  assert.equal(clamped.page, 3)
  assert.deepEqual(clamped.items, rows.slice(40))

  const empty = paginateItems([], 2, 20)
  assert.deepEqual(empty, {
    items: [],
    page: 1,
    pageSize: 20,
    total: 0,
    totalPages: 1,
    startIndex: 0,
    endIndex: 0,
  })
}

// 4. 构造完整 FinancialEvaluationBlock 样本并校验全部图表 mapper
const sampleFin = {
  status: 'evaluated',
  evaluation_years: 3,
  construction_period: 1,
  operation_years: 2,
  currency: 'CNY',
  price_basis: 'tax_exclusive',
  base_year: 2026,
  discount_rate: 0.08,
  construction_total: '100.00',
  construction_total_unit: 'CNY',
  annual_fixed_om: '5.00',
  payments_applied: [
    { t: 0, ratio: '0.6', amount: '60.00' },
    { t: 1, ratio: '0.4', amount: '40.00' },
  ],
  net_cashflows: [],
  construction_units: [],
  cost_items: Array.from({ length: 35 }, (_, i) => ({
    instance_id: `dev_${i}`,
    finance_type: 'shared',
    component: 'upfront_capex',
    item_id: `item_${i}`,
    axis: 'scalar',
    period: 0,
    raw: `${i + 1}.000000000000000001`,
    gross: `${i + 2}.13`,
    deductible_input: '1.13',
    net_basis: `${i + 1}.000000000000000001`,
    unit: 'CNY',
  })),
  amounts: [
    { id: 'capex_sum', axis: 'scalar', unit: 'CNY', values: [{ period: 0, value: '123.450000000000000001' }] },
    { id: 'ann_fee', axis: 'financial_year', unit: 'CNY/a', values: [{ period: 2, value: '12.5' }, { period: 3, value: '15.0' }] },
  ],
  cash_movements: Array.from({ length: 26 }, (_, i) => ({
    id: `mv_${i}`,
    axis: 'financial_year',
    period: (i % 3) + 1,
    direction: 'outflow',
    price_basis: 'tax_exclusive',
    raw: `${10 + i}.0001`,
    gross: `${11 + i}.0001`,
    deductible_input: '1',
    net_basis: `${10 + i}.0001`,
    output_tax: '0',
    net_revenue: `${11 + i}.0001`,
    payments: [{ at: { kind: 'financial_year', year: 1 }, ratio: '1', amount: `${11 + i}.0001` }],
    end_unpaid: '0',
  })),
  assets: [
    {
      id: 'asset_hp',
      basis: '100',
      capitalized_at: { kind: 'financial_year', year: 1 },
      available_from: 2,
      book_values: [
        { year: 2, value: '75.50' },
        { year: 3, value: '50.50' },
      ],
    },
  ],
  annual_operation: [
    { financial_year: 2, energy_net: '80.00', variable_om: '5.00', fixed_om: '5.00', maintenance: '0', demand_charge: '10.00', net: '60.00' },
    { financial_year: 3, energy_net: '90.00', variable_om: '6.00', fixed_om: '5.00', maintenance: '2.00', demand_charge: '12.00', net: '65.00' },
  ],
  monthly_demand: [
    { billing_id: 'grid_a', charge_id: 'd1', calendar_year: 2026, calendar_month: 2, financial_year: 2, peak_power: '55.5', peak_unit: 'kW', rate: { value: '30', unit: 'CNY/kW' }, charge: '1665.00' },
    { billing_id: 'grid_b', charge_id: 'd2', calendar_year: 2026, calendar_month: 1, financial_year: 2, peak_power: '20.0', peak_unit: 'kW', rate: { value: '10', unit: 'CNY/kW' }, charge: '200.00' },
  ],
  tax_cash: {
    vat: [
      { pool: 'p_main', month: 1, paid: '0', refund_claimed: '5', refund_arrived: '0', carry: '13.0000000000000001', receivable: '0' },
      { pool: 'p_main', month: 2, paid: '4.5', refund_claimed: '0', refund_arrived: '5', carry: '0', receivable: '0' },
      { pool: 'p_alt', month: 1, paid: '1.25', refund_claimed: '0', refund_arrived: '0', carry: '0', receivable: '0' },
    ],
    vat_rules: [],
    refunds: [],
    income: [
      { rule: 'cit_std', year: 2, taxable_base: '30.00', loss_used: '0', loss_generated: '0', loss_expired: '0', assessed: '7.50', tax: '7.50' },
      { rule: 'cit_alt', year: 3, taxable_base: '40.00', loss_used: '5', loss_generated: '0', loss_expired: '0', assessed: '8.75', tax: '8.75' },
    ],
    depreciation: [
      { rule: 'cit_std', base_from: { kind: 'upfront_capex_net', finance_type: 'heat_pump' }, year: 2, amount: '24.50' },
    ],
    loss_balances: [],
    derived: [],
  },
  annual_net_flows: ['-60.00', '-40.00', '52.50', '56.25'],
  comparison: {
    mode: 'difference',
    baseline_source: { kind: 'task_result', task_id: 1, evidence_id: 2, attempt_id: 3, object_id: 4 },
    comparability: { service_basis: 'same heat', differences: 'none' },
    facts: {},
    baseline_net_flows: ['-10.00', '-10.00', '20.00', '20.00'],
    evaluation_net_flows: ['-50.000000000000001', '-30.00', '32.50', '36.25'],
  },
  evaluation: {
    rows: [
      { year: 0, net: '-60.000000000000001', discounted: '-60.00', cumulative: '-60.00', cumulative_discounted: '-60.00' },
      { year: 1, net: '-40.00', discounted: '-37.04', cumulative: '-100.00', cumulative_discounted: '-97.04' },
      { year: 2, net: '52.50', discounted: '45.01', cumulative: '-47.50', cumulative_discounted: '-52.03' },
      { year: 3, net: '56.25', discounted: '44.65', cumulative: '8.75', cumulative_discounted: '-7.38' },
    ],
    npv: '-7.38',
    npv_unit: 'CNY',
    irr: 0.045,
    irr_status: 'normal',
    static_payback: 2.84,
    static_status: 'recovered',
    dynamic_payback: null,
    dynamic_status: 'not_recovered',
  },
}

const snapshotBefore = JSON.stringify(sampleFin)

const cfChart = buildCashflowChartData(sampleFin)
assert.deepEqual(cfChart.categories, ['t=0', 't=1', 't=2', 't=3'])
assert.equal(cfChart.bars[0].rawValues?.[0], '-60.000000000000001')

const opChart = buildAnnualOperationChartData(sampleFin)
assert.deepEqual(opChart.categories, ['t=2', 't=3'])
assert.equal(opChart.series.length, 6)

const demandA = buildMonthlyDemandChartData(sampleFin)
assert.deepEqual(demandA.billingIds, ['grid_a', 'grid_b'])
assert.equal(demandA.activeBillingId, 'grid_a')
assert.equal(demandA.categories[0], '2026-02')
const demandB = buildMonthlyDemandChartData(sampleFin, 'grid_b')
assert.equal(demandB.activeBillingId, 'grid_b')
assert.equal(demandB.chargeSeries[0].rawValues?.[0], '200.00')

const vatMain = buildVatPoolChartData(sampleFin)
assert.deepEqual(vatMain.pools, ['p_main', 'p_alt'])
assert.equal(vatMain.series[2].rawValues?.[0], '13.0000000000000001')

const incAlt = buildIncomeTaxChartData(sampleFin, 'cit_alt')
assert.equal(incAlt.activeRule, 'cit_alt')
assert.equal(incAlt.series[2].rawValues?.[0], '8.75')

const depChart = buildDepreciationChartData(sampleFin)
assert.equal(depChart.series[0].label, 'cit_std:heat_pump')
assert.equal(depChart.series[0].rawValues?.[0], '24.50')

const cmpChart = buildComparisonChartData(sampleFin)
assert.equal(cmpChart.series[2].rawValues?.[0], '-50.000000000000001')

const assetChart = buildAssetBookValueChartData(sampleFin)
assert.deepEqual(assetChart.categories, ['y=2', 'y=3'])
assert.equal(assetChart.series[0].rawValues?.[0], '75.50')

const amountGroups = buildNamedAmountChartGroups(sampleFin.amounts)
assert.equal(amountGroups.length, 2)
assert.equal(amountGroups[0].series[0].rawValues?.[0], '123.450000000000000001')

const costGroups = buildCostItemChartGroups(sampleFin.cost_items, 30)
assert.equal(costGroups[0].truncated, true)
assert.equal(costGroups[0].totalCount, 35)
assert.equal(costGroups[0].categories.length, 30)

assert.equal(JSON.stringify(sampleFin), snapshotBefore, 'chart mappers never mutate sealed payload')

// 5. 组件 SSR 渲染断言（Tab / Pagination / ComboChart / BarChart / CategoryLineChart / LineChart toggleable / 分页表格）
const rendered = renderToStaticMarkup(
  createElement(
    I18nProvider,
    null,
    createElement(
      'div',
      null,
      createElement(Tabs, {
        ariaLabel: 'Result sections',
        activeId: 'financial',
        onChange: () => {},
        items: [
          { id: 'overview', label: '总览' },
          { id: 'financial', label: '财务与现金流', badge: 2 },
        ],
      }),
      createElement(ComboChart, {
        categories: cfChart.categories,
        bars: cfChart.bars,
        lines: cfChart.lines,
        ariaLabel: 'CF Combo',
        unit: 'CNY',
      }),
      createElement(BarChart, {
        categories: opChart.categories,
        series: opChart.series,
        ariaLabel: 'Op Bar',
        unit: 'CNY',
      }),
      createElement(CategoryLineChart, {
        categories: vatMain.categories,
        series: vatMain.series,
        ariaLabel: 'VAT Line',
        unit: 'CNY',
      }),
      createElement(LineChart, {
        series: [{ key: 'hp1.p', label: 'hp1.p', color: 'var(--ies-primary)', values: [1, 2, 3] }],
        ariaLabel: 'Steps',
        toggleable: true,
      }),
      createElement(Pagination, {
        page: 2,
        pageSize: 20,
        total: 53,
        onPageChange: () => {},
        onPageSizeChange: () => {},
      }),
      createElement(FinancialRelations, {
        costItems: sampleFin.cost_items,
        amounts: sampleFin.amounts,
        paginated: true,
        defaultPageSize: 12,
      }),
      createElement(FinancialTransactions, {
        fin: sampleFin,
        sections: ['movements'],
        paginated: true,
        defaultPageSize: 12,
      }),
      createElement(FinancialComparison, {
        fin: {
          ...sampleFin,
          annual_net_flows: Array.from({ length: 25 }, (_, i) => `${i}`),
          comparison: {
            ...sampleFin.comparison,
            baseline_net_flows: Array.from({ length: 25 }, () => '1'),
            evaluation_net_flows: Array.from({ length: 25 }, (_, i) => `${i - 1}`),
          },
        },
        paginated: true,
        defaultPageSize: 12,
      }),
    ),
  ),
)

assert.ok(rendered.includes('role="tablist"'), 'Tabs renders semantic tablist')
assert.ok(rendered.includes('role="tab"'), 'Tabs renders semantic tab buttons')
assert.ok(rendered.includes('-60.000000000000001 CNY'), 'ComboChart preserves exact sealed decimal in title')
assert.ok(rendered.includes('<rect'), 'BarChart/ComboChart renders rect bars')
assert.ok(rendered.includes('<polyline'), 'ComboChart/CategoryLineChart/LineChart renders polylines')
assert.ok(rendered.includes('aria-pressed="true"'), 'LineChart/CategoryLineChart renders toggleable legend buttons')
assert.ok(rendered.includes('第 2 / 3 页 · 共 53 条'), 'Pagination renders summary text')
assert.ok(rendered.includes('dev_0'), 'Paginated FinancialRelations renders page 1 row')
assert.ok(!rendered.includes('dev_29'), 'Paginated FinancialRelations slices rows beyond page 1')
assert.ok(rendered.includes('mv_0 / financial_year=1'), 'Paginated FinancialTransactions renders page 1 row')
assert.ok(!rendered.includes('mv_25 /'), 'Paginated FinancialTransactions slices rows beyond page 1')

console.log('Results charts, navigation, mappers, and pagination tests passed')
