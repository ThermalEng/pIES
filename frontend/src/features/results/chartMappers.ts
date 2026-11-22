/**
 * 结果页纯视图映射与导航状态工具。
 *
 * 规则(遵循 ARCHITECTURE_CONSTITUTION / frontend.md):
 * - 绝不重算、加总或相减任何财务/税务金额或比率；
 * - 原始封存十进制字符串原样保存在 rawValues，数值解析仅用于 SVG 坐标绘制；
 * - 未知或非有限值保持 null，永不伪造为 0。
 */

import { parseChartNumber } from '../../components/charts'
import type { CategorySeries } from '../../components/charts'
import type {
  FinancialCostItem,
  FinancialEvaluationBlock,
  FinancialNamedAmount,
} from '../../types'

export type ResultTab = 'overview' | 'financial' | 'tax' | 'engineering' | 'assessment'

export type ResultDetail =
  | 'cashflows'
  | 'operation'
  | 'comparison'
  | 'demand'
  | 'vat'
  | 'income'
  | 'relations'
  | 'transactions'
  | 'hourly'

export const DETAIL_PARENT_TAB: Record<ResultDetail, ResultTab> = {
  cashflows: 'financial',
  operation: 'financial',
  comparison: 'financial',
  demand: 'tax',
  vat: 'tax',
  income: 'tax',
  relations: 'tax',
  transactions: 'tax',
  hourly: 'engineering',
}

const VALID_TABS = new Set<ResultTab>(['overview', 'financial', 'tax', 'engineering', 'assessment'])

/** 解析结果页 URL 查询参数；无效值安全回落到 overview/null。 */
export function parseResultNavigation(params: URLSearchParams): {
  tab: ResultTab
  detail: ResultDetail | null
} {
  const rawDetail = params.get('detail') as ResultDetail | null
  if (rawDetail && rawDetail in DETAIL_PARENT_TAB) {
    return { tab: DETAIL_PARENT_TAB[rawDetail], detail: rawDetail }
  }
  const rawTab = params.get('tab') as ResultTab | null
  if (rawTab && VALID_TABS.has(rawTab)) {
    return { tab: rawTab, detail: null }
  }
  return { tab: 'overview', detail: null }
}

/** 生成结果页导航查询参数，同时移除已消费的一次性 ?package=。 */
export function buildResultSearchParams(
  current: URLSearchParams,
  next: { tab: ResultTab; detail?: ResultDetail | null },
): URLSearchParams {
  const out = new URLSearchParams(current)
  out.delete('package')
  const detail = next.detail ?? null
  const tab = detail ? DETAIL_PARENT_TAB[detail] : next.tab
  if (detail) {
    out.set('tab', tab)
    out.set('detail', detail)
  } else {
    out.delete('detail')
    if (tab === 'overview') out.delete('tab')
    else out.set('tab', tab)
  }
  return out
}

export interface PaginatedSlice<T> {
  items: T[]
  page: number
  pageSize: number
  total: number
  totalPages: number
  startIndex: number
  endIndex: number
}

/** 通用客户端列表分页器（固定顺序数组切片，不重排后端权威顺序）。 */
export function paginateItems<T>(
  items: readonly T[],
  requestedPage: number,
  requestedPageSize: number,
): PaginatedSlice<T> {
  const pageSize = Math.max(1, Math.floor(requestedPageSize) || 20)
  const total = items.length
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const page = Math.min(Math.max(1, Math.floor(requestedPage) || 1), totalPages)
  if (total === 0) {
    return { items: [], page: 1, pageSize, total: 0, totalPages: 1, startIndex: 0, endIndex: 0 }
  }
  const startIndex = (page - 1) * pageSize
  const endIndex = Math.min(total, startIndex + pageSize)
  return {
    items: items.slice(startIndex, endIndex),
    page,
    pageSize,
    total,
    totalPages,
    startIndex,
    endIndex,
  }
}

function toSeriesData(
  items: readonly (string | number | null | undefined)[],
): { values: (number | null)[]; rawValues: (string | null)[] } {
  const values: (number | null)[] = []
  const rawValues: (string | null)[] = []
  for (const item of items) {
    values.push(parseChartNumber(item))
    if (item === null || item === undefined) rawValues.push(null)
    else rawValues.push(String(item))
  }
  return { values, rawValues }
}

const CHART_COLORS = [
  'var(--ies-primary)',
  'var(--ies-success)',
  'var(--ies-warning)',
  'var(--ies-danger)',
  'var(--ies-chart-purple)',
  'var(--ies-chart-teal)',
]

/** 构造 CF[0..E] 现金流组合图数据（柱：当期净额/折现；线：累计/累计折现）。 */
export function buildCashflowChartData(fin: FinancialEvaluationBlock): {
  categories: string[]
  bars: CategorySeries[]
  lines: CategorySeries[]
} {
  const rows = fin.evaluation.rows
  if (rows.length > 0) {
    const categories = rows.map((r) => `t=${r.year}`)
    const netData = toSeriesData(rows.map((r) => r.net))
    const discData = toSeriesData(rows.map((r) => r.discounted))
    const cumData = toSeriesData(rows.map((r) => r.cumulative))
    const cumDiscData = toSeriesData(rows.map((r) => r.cumulative_discounted))
    return {
      categories,
      bars: [
        {
          key: 'net',
          label: 'net',
          color: 'var(--ies-primary)',
          positiveColor: 'var(--ies-success)',
          negativeColor: 'var(--ies-danger)',
          ...netData,
        },
        {
          key: 'discounted',
          label: 'discounted',
          color: 'var(--ies-brand-secondary)',
          ...discData,
        },
      ],
      lines: [
        {
          key: 'cumulative',
          label: 'cumulative',
          color: 'var(--ies-warning)',
          ...cumData,
        },
        {
          key: 'cumulative_discounted',
          label: 'cumulative_discounted',
          color: 'var(--ies-chart-purple)',
          dashed: true,
          ...cumDiscData,
        },
      ],
    }
  }
  if (fin.annual_net_flows.length > 0) {
    const categories = fin.annual_net_flows.map((_, idx) => `t=${idx}`)
    const netData = toSeriesData(fin.annual_net_flows)
    return {
      categories,
      bars: [
        {
          key: 'net',
          label: 'net',
          color: 'var(--ies-primary)',
          positiveColor: 'var(--ies-success)',
          negativeColor: 'var(--ies-danger)',
          ...netData,
        },
      ],
      lines: [],
    }
  }
  return { categories: [], bars: [], lines: [] }
}

/** 构造运营年度分量图数据（按年分项柱形，原值直通不重算）。 */
export function buildAnnualOperationChartData(fin: FinancialEvaluationBlock): {
  categories: string[]
  series: CategorySeries[]
} {
  const annual = fin.annual_operation
  if (annual.length === 0) return { categories: [], series: [] }
  const categories = annual.map((a) => `t=${a.financial_year}`)
  return {
    categories,
    series: [
      { key: 'energy_net', label: 'energy_net', color: 'var(--ies-primary)', ...toSeriesData(annual.map((a) => a.energy_net)) },
      { key: 'variable_om', label: 'variable_om', color: 'var(--ies-brand-secondary)', ...toSeriesData(annual.map((a) => a.variable_om)) },
      { key: 'fixed_om', label: 'fixed_om', color: 'var(--ies-warning)', ...toSeriesData(annual.map((a) => a.fixed_om)) },
      { key: 'maintenance', label: 'maintenance', color: 'var(--ies-chart-purple)', ...toSeriesData(annual.map((a) => a.maintenance)) },
      { key: 'demand_charge', label: 'demand_charge', color: 'var(--ies-danger)', ...toSeriesData(annual.map((a) => a.demand_charge)) },
      { key: 'net', label: 'net', color: 'var(--ies-success)', ...toSeriesData(annual.map((a) => a.net)) },
    ],
  }
}

/** 构造单条计量绑定的自然月需量峰功率与需量费图表数据。 */
export function buildMonthlyDemandChartData(
  fin: FinancialEvaluationBlock,
  preferredBindingId?: string | null,
): {
  billingIds: string[]
  activeBillingId: string | null
  peakUnit: string
  categories: string[]
  peakSeries: CategorySeries[]
  chargeSeries: CategorySeries[]
} {
  const all = fin.monthly_demand
  if (all.length === 0) {
    return { billingIds: [], activeBillingId: null, peakUnit: '', categories: [], peakSeries: [], chargeSeries: [] }
  }
  const billingIds = Array.from(new Set(all.map((r) => r.billing_id)))
  const activeBillingId =
    preferredBindingId && billingIds.includes(preferredBindingId) ? preferredBindingId : billingIds[0]
  const rows = all
    .filter((r) => r.billing_id === activeBillingId)
    .sort((a, b) => a.calendar_year - b.calendar_year || a.calendar_month - b.calendar_month)
  const categories = rows.map((r) => `${r.calendar_year}-${String(r.calendar_month).padStart(2, '0')}`)
  const peakUnit = rows[0]?.peak_unit ?? ''
  return {
    billingIds,
    activeBillingId,
    peakUnit,
    categories,
    peakSeries: [
      {
        key: 'peak_power',
        label: 'peak_power',
        color: 'var(--ies-primary)',
        ...toSeriesData(rows.map((r) => r.peak_power)),
      },
    ],
    chargeSeries: [
      {
        key: 'charge',
        label: 'charge',
        color: 'var(--ies-warning)',
        ...toSeriesData(rows.map((r) => r.charge)),
      },
    ],
  }
}

/** 构造单个共享抵扣池的按月增值税过程图表数据（实缴/退税到账/留抵/应收）。 */
export function buildVatPoolChartData(
  fin: FinancialEvaluationBlock,
  preferredPool?: string | null,
): {
  pools: string[]
  activePool: string | null
  categories: string[]
  series: CategorySeries[]
} {
  const vat = fin.tax_cash.vat
  if (vat.length === 0) {
    return { pools: [], activePool: null, categories: [], series: [] }
  }
  const pools = Array.from(new Set(vat.map((r) => r.pool)))
  const activePool = preferredPool && pools.includes(preferredPool) ? preferredPool : pools[0]
  const rows = vat.filter((r) => r.pool === activePool).sort((a, b) => a.month - b.month)
  const categories = rows.map((r) => `m=${r.month}`)
  return {
    pools,
    activePool,
    categories,
    series: [
      { key: 'paid', label: 'paid', color: 'var(--ies-danger)', ...toSeriesData(rows.map((r) => r.paid)) },
      { key: 'refund_arrived', label: 'refund_arrived', color: 'var(--ies-success)', ...toSeriesData(rows.map((r) => r.refund_arrived)) },
      { key: 'carry', label: 'carry', color: 'var(--ies-primary)', ...toSeriesData(rows.map((r) => r.carry)) },
      { key: 'receivable', label: 'receivable', color: 'var(--ies-warning)', dashed: true, ...toSeriesData(rows.map((r) => r.receivable)) },
    ],
  }
}

/** 构造单个所得税规则的逐年税基/抵亏/实缴税额图数据。 */
export function buildIncomeTaxChartData(
  fin: FinancialEvaluationBlock,
  preferredRule?: string | null,
): {
  rules: string[]
  activeRule: string | null
  categories: string[]
  series: CategorySeries[]
} {
  const income = fin.tax_cash.income
  if (income.length === 0) {
    return { rules: [], activeRule: null, categories: [], series: [] }
  }
  const rules = Array.from(new Set(income.map((r) => r.rule)))
  const activeRule = preferredRule && rules.includes(preferredRule) ? preferredRule : rules[0]
  const rows = income.filter((r) => r.rule === activeRule).sort((a, b) => a.year - b.year)
  const categories = rows.map((r) => `y=${r.year}`)
  return {
    rules,
    activeRule,
    categories,
    series: [
      { key: 'taxable_base', label: 'taxable_base', color: 'var(--ies-primary)', ...toSeriesData(rows.map((r) => r.taxable_base)) },
      { key: 'loss_used', label: 'loss_used', color: 'var(--ies-warning)', ...toSeriesData(rows.map((r) => r.loss_used)) },
      { key: 'tax', label: 'tax', color: 'var(--ies-danger)', ...toSeriesData(rows.map((r) => r.tax)) },
    ],
  }
}

function depreciationSourceLabel(row: FinancialEvaluationBlock['tax_cash']['depreciation'][number]): string {
  const bf = row.base_from
  if (bf.kind === 'upfront_capex_net') return `${row.rule}:${bf.finance_type}`
  if (bf.kind === 'asset') return `${row.rule}:${bf.id}`
  return `${row.rule}:y=${bf.year}#${bf.position}`
}

/** 构造计税折旧按身份分组的逐年柱状图数据（不跨资产或跨规则求和）。 */
export function buildDepreciationChartData(fin: FinancialEvaluationBlock): {
  categories: string[]
  series: CategorySeries[]
} {
  const dep = fin.tax_cash.depreciation
  if (dep.length === 0) return { categories: [], series: [] }
  const years = Array.from(new Set(dep.map((r) => r.year))).sort((a, b) => a - b)
  const categories = years.map((y) => `y=${y}`)
  const bySource = new Map<string, Map<number, string>>()
  for (const row of dep) {
    const id = depreciationSourceLabel(row)
    let map = bySource.get(id)
    if (!map) {
      map = new Map<number, string>()
      bySource.set(id, map)
    }
    map.set(row.year, row.amount)
  }
  const series: CategorySeries[] = []
  let idx = 0
  for (const [id, yearMap] of bySource.entries()) {
    const rawValues = years.map((yr) => yearMap.get(yr) ?? null)
    series.push({
      key: id,
      label: id,
      color: CHART_COLORS[idx % CHART_COLORS.length],
      values: rawValues.map((v) => parseChartNumber(v)),
      rawValues,
    })
    idx += 1
  }
  return { categories, series }
}

/** 构造差额/基准比较折线图数据（仅 difference 模式，严格读取封存三组数组）。 */
export function buildComparisonChartData(fin: FinancialEvaluationBlock): {
  categories: string[]
  series: CategorySeries[]
} {
  if (fin.comparison.mode !== 'difference') return { categories: [], series: [] }
  const len = fin.annual_net_flows.length
  if (len === 0) return { categories: [], series: [] }
  const categories = Array.from({ length: len }, (_, i) => `t=${i}`)
  return {
    categories,
    series: [
      { key: 'candidate', label: 'candidate', color: 'var(--ies-primary)', ...toSeriesData(fin.annual_net_flows) },
      { key: 'baseline', label: 'baseline', color: 'var(--ies-neutral)', dashed: true, ...toSeriesData(fin.comparison.baseline_net_flows) },
      { key: 'difference', label: 'difference', color: 'var(--ies-success)', ...toSeriesData(fin.comparison.evaluation_net_flows) },
    ],
  }
}

/** 构造资产逐年账面值折线图数据。 */
export function buildAssetBookValueChartData(fin: FinancialEvaluationBlock): {
  categories: string[]
  series: CategorySeries[]
} {
  const assets = fin.assets
  if (assets.length === 0) return { categories: [], series: [] }
  const yearsSet = new Set<number>()
  for (const asset of assets) {
    for (const bv of asset.book_values) yearsSet.add(bv.year)
  }
  const years = Array.from(yearsSet).sort((a, b) => a - b)
  if (years.length === 0) return { categories: [], series: [] }
  const categories = years.map((yr) => `y=${yr}`)
  const series: CategorySeries[] = assets.map((asset, idx) => {
    const byYear = new Map(asset.book_values.map((bv) => [bv.year, bv.value]))
    const rawValues = years.map((yr) => byYear.get(yr) ?? null)
    return {
      key: asset.id,
      label: asset.id,
      color: CHART_COLORS[idx % CHART_COLORS.length],
      values: rawValues.map((v) => parseChartNumber(v)),
      rawValues,
    }
  })
  return { categories, series }
}

export interface NamedAmountChartGroup {
  axis: 'scalar' | 'financial_year' | 'tax_month'
  unit: string
  categories: string[]
  series: CategorySeries[]
}

/** 按 (axis, unit) 分组构造命名金额可视化图数据，绝不跨单位或跨轴混算。 */
export function buildNamedAmountChartGroups(amounts: readonly FinancialNamedAmount[]): NamedAmountChartGroup[] {
  if (amounts.length === 0) return []
  const byGroup = new Map<string, { axis: FinancialNamedAmount['axis']; unit: string; list: FinancialNamedAmount[] }>()
  for (const a of amounts) {
    const key = `${a.axis}\0${a.unit}`
    let g = byGroup.get(key)
    if (!g) {
      g = { axis: a.axis, unit: a.unit, list: [] }
      byGroup.set(key, g)
    }
    g.list.push(a)
  }
  const groups: NamedAmountChartGroup[] = []
  for (const { axis, unit, list } of byGroup.values()) {
    if (axis === 'scalar') {
      const categories = list.map((a) => a.id)
      const rawValues = list.map((a) => a.values[0]?.value ?? null)
      groups.push({
        axis,
        unit,
        categories,
        series: [{
          key: 'scalar',
          label: unit,
          color: 'var(--ies-primary)',
          positiveColor: 'var(--ies-primary)',
          negativeColor: 'var(--ies-danger)',
          values: rawValues.map((v) => parseChartNumber(v)),
          rawValues,
        }],
      })
    } else {
      const prefix = axis === 'financial_year' ? 'y=' : 'm='
      const periods = Array.from(new Set(list.flatMap((a) => a.values.map((v) => v.period)))).sort((a, b) => a - b)
      if (periods.length === 0) continue
      const categories = periods.map((p) => `${prefix}${p}`)
      const series: CategorySeries[] = list.map((a, idx) => {
        const byPeriod = new Map(a.values.map((v) => [v.period, v.value]))
        const rawValues = periods.map((p) => byPeriod.get(p) ?? null)
        return {
          key: a.id,
          label: a.id,
          color: CHART_COLORS[idx % CHART_COLORS.length],
          values: rawValues.map((v) => parseChartNumber(v)),
          rawValues,
        }
      })
      groups.push({ axis, unit, categories, series })
    }
  }
  return groups
}

export interface CostItemChartGroup {
  unit: string
  totalCount: number
  truncated: boolean
  categories: string[]
  series: CategorySeries[]
}

/** 按单位分组构造命名成本明细图数据（单图最多展示 30 条，完整列表见二级页）。 */
export function buildCostItemChartGroups(
  costItems: readonly FinancialCostItem[],
  maxBars = 30,
): CostItemChartGroup[] {
  if (costItems.length === 0) return []
  const byUnit = new Map<string, FinancialCostItem[]>()
  for (const item of costItems) {
    let arr = byUnit.get(item.unit)
    if (!arr) {
      arr = []
      byUnit.set(item.unit, arr)
    }
    arr.push(item)
  }
  const out: CostItemChartGroup[] = []
  for (const [unit, items] of byUnit.entries()) {
    const truncated = items.length > maxBars
    const visible = items.slice(0, maxBars)
    const categories = visible.map((it) => `${it.instance_id}.${it.item_id}${it.axis === 'scalar' ? '' : `(${it.period})`}`)
    out.push({
      unit,
      totalCount: items.length,
      truncated,
      categories,
      series: [
        { key: 'net_basis', label: 'net_basis', color: 'var(--ies-primary)', ...toSeriesData(visible.map((it) => it.net_basis)) },
        { key: 'gross', label: 'gross', color: 'var(--ies-brand-secondary)', ...toSeriesData(visible.map((it) => it.gross)) },
      ],
    })
  }
  return out
}
