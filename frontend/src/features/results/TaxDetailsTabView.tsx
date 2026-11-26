/**
 * 结果页「过程与税务」Tab（一级可视化：需量/VAT/所得税折旧/命名明细/收付资产图表 + 二级数据页入口）。
 */

import { useState } from 'react'

import { BarChart, CategoryLineChart } from '../../components/charts'
import { Badge, Button, Card, EmptyState, Select } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { FinancialAbsence, FinancialEvaluationBlock, TaskOutcome } from '../../types'
import { FinancialAbsenceAlert } from './resultHelpers'
import {
  asObjectArray,
  buildAssetBookValueChartData,
  buildCostItemChartGroups,
  buildDepreciationChartData,
  buildIncomeTaxChartData,
  buildMonthlyDemandChartData,
  buildNamedAmountChartGroups,
  buildVatPoolChartData,
} from './chartMappers'
import type { ResultDetail } from './chartMappers'

export interface TaxDetailsTabViewProps {
  fin: FinancialEvaluationBlock | null
  finAbsence: FinancialAbsence
  outcome: TaskOutcome | null
  onOpenDetail: (detail: ResultDetail) => void
}

export function TaxDetailsTabView({
  fin,
  finAbsence,
  outcome,
  onOpenDetail,
}: TaxDetailsTabViewProps) {
  const { t } = useI18n()
  const [selectedBinding, setSelectedBinding] = useState<string>('')
  const [selectedPool, setSelectedPool] = useState<string>('')
  const [selectedRule, setSelectedRule] = useState<string>('')

  if (!fin) {
    if (finAbsence) {
      return <FinancialAbsenceAlert finAbsence={finAbsence} outcome={outcome} />
    }
    return (
      <Card title={t('ies.result.tab_tax')} className="ies-section">
        <EmptyState icon="info" title={t('ies.result.detail_unavailable')} />
      </Card>
    )
  }

  const ccy = fin.currency ?? ''
  const monthlyDemand = asObjectArray<FinancialEvaluationBlock['monthly_demand'][number]>(
    fin.monthly_demand,
  )
  const costItems = asObjectArray<FinancialEvaluationBlock['cost_items'][number]>(fin.cost_items)
  const amounts = asObjectArray<FinancialEvaluationBlock['amounts'][number]>(fin.amounts)
  const cashMovements = asObjectArray<FinancialEvaluationBlock['cash_movements'][number]>(
    fin.cash_movements,
  )
  const assets = asObjectArray<FinancialEvaluationBlock['assets'][number]>(fin.assets)
  const taxCash = fin.tax_cash && typeof fin.tax_cash === 'object' ? fin.tax_cash : null
  const vat = asObjectArray<FinancialEvaluationBlock['tax_cash']['vat'][number]>(taxCash?.vat)
  const vatRules = asObjectArray<FinancialEvaluationBlock['tax_cash']['vat_rules'][number]>(
    taxCash?.vat_rules,
  )
  const refunds = asObjectArray<FinancialEvaluationBlock['tax_cash']['refunds'][number]>(
    taxCash?.refunds,
  )
  const income = asObjectArray<FinancialEvaluationBlock['tax_cash']['income'][number]>(
    taxCash?.income,
  )
  const depreciation = asObjectArray<FinancialEvaluationBlock['tax_cash']['depreciation'][number]>(
    taxCash?.depreciation,
  )
  const lossBalances = asObjectArray<FinancialEvaluationBlock['tax_cash']['loss_balances'][number]>(
    taxCash?.loss_balances,
  )
  const derived = asObjectArray<FinancialEvaluationBlock['tax_cash']['derived'][number]>(
    taxCash?.derived,
  )

  const demandChart = buildMonthlyDemandChartData(fin, selectedBinding || undefined)
  const vatChart = buildVatPoolChartData(fin, selectedPool || undefined)
  const incomeChart = buildIncomeTaxChartData(fin, selectedRule || undefined)
  const depChart = buildDepreciationChartData(fin)
  const costGroups = buildCostItemChartGroups(costItems, 30)
  const amountGroups = buildNamedAmountChartGroups(amounts)
  const assetChart = buildAssetBookValueChartData(fin)

  const vatTotal = vat.length + vatRules.length + refunds.length
  const incomeTotal =
    income.length +
    depreciation.length +
    lossBalances.length +
    derived.length
  const amountRowCount = amounts.reduce(
    (acc, a) => acc + asObjectArray(a.values).length,
    0,
  )
  const relationsTotal = costItems.length + amountRowCount
  const transactionsTotal = cashMovements.length + assets.length

  const vatLabelMap: Record<string, string> = {
    paid: t('ies.result.fin_tax_paid'),
    refund_arrived: t('ies.result.fin_tax_arrived'),
    carry: t('ies.result.fin_tax_carry'),
    receivable: t('ies.result.fin_tax_receivable'),
  }
  const incomeLabelMap: Record<string, string> = {
    taxable_base: t('ies.result.fin_tax_base'),
    loss_used: t('ies.result.fin_tax_used'),
    tax: t('ies.result.fin_tax_tax'),
  }
  const costLabelMap: Record<string, string> = {
    net_basis: t('ies.result.fin_relation_net_basis'),
    gross: t('ies.result.fin_relation_gross'),
  }

  return (
    <>
      {/* 1. 自然月需量账单 */}
      <Card
        title={t('ies.result.fin_demand_monthly')}
        actions={
          monthlyDemand.length > 0 ? (
            <div className="ies-form-row">
              {demandChart.billingIds.length > 1 ? (
                <Select
                  aria-label={t('ies.result.filter_binding')}
                  value={demandChart.activeBillingId ?? ''}
                  onChange={(e) => setSelectedBinding(e.target.value)}
                >
                  {demandChart.billingIds.map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </Select>
              ) : null}
              <Button variant="secondary" size="sm" onClick={() => onOpenDetail('demand')}>
                {t('ies.result.view_detail_data', { count: monthlyDemand.length })}
              </Button>
            </div>
          ) : undefined
        }
        className="ies-section"
      >
        {monthlyDemand.length === 0 ? (
          <p className="ies-text-secondary">
            {t('ies.result.fin_demand_unbound')}
          </p>
        ) : (
          <>
            <div className="ies-chart-grid">
              <div>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.demand_peak_chart')}
                </h3>
                <CategoryLineChart
                  categories={demandChart.categories}
                  series={demandChart.peakSeries.map((s) => ({
                    ...s,
                    label: t('ies.result.fin_demand_peak'),
                  }))}
                  ariaLabel={t('ies.result.demand_peak_chart')}
                  yLabel={demandChart.peakUnit}
                  unit={demandChart.peakUnit}
                  toggleable={false}
                  hoverHint={t('ies.result.chart_hover_hint')}
                />
              </div>
              <div>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.demand_charge_chart')}
                </h3>
                <BarChart
                  categories={demandChart.categories}
                  series={demandChart.chargeSeries.map((s) => ({
                    ...s,
                    label: t('ies.result.fin_demand_amount'),
                  }))}
                  ariaLabel={t('ies.result.demand_charge_chart')}
                  yLabel={ccy}
                  unit={ccy}
                  hoverHint={t('ies.result.chart_hover_hint')}
                />
              </div>
            </div>
            <p className="ies-text-note" style={{ marginTop: 'var(--ies-space-2)' }}>
              {t('ies.result.fin_demand_zero_note')}
            </p>
          </>
        )}
      </Card>

      {/* 2. 增值税与退款 */}
      <Card
        title={t('ies.result.tax_vat_card')}
        actions={
          <div className="ies-form-row">
            {vatChart.pools.length > 1 ? (
              <Select
                aria-label={t('ies.result.filter_pool')}
                value={vatChart.activePool ?? ''}
                onChange={(e) => setSelectedPool(e.target.value)}
              >
                {vatChart.pools.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </Select>
            ) : null}
            <Button variant="secondary" size="sm" onClick={() => onOpenDetail('vat')}>
              {t('ies.result.view_detail_data', { count: vatTotal })}
            </Button>
          </div>
        }
        className="ies-section"
      >
        <div className="ies-form-row" style={{ marginBottom: 'var(--ies-space-3)' }}>
          <Badge variant="neutral" label={`${t('ies.result.tax_vat_pool_tab')}: ${vat.length}`} />
          <Badge variant="neutral" label={t('ies.result.fin_tax_rule_count', { count: vatRules.length })} />
          <Badge variant="neutral" label={t('ies.result.fin_tax_refund_count', { count: refunds.length })} />
        </div>
        {vatChart.categories.length > 0 ? (
          <CategoryLineChart
            categories={vatChart.categories}
            series={vatChart.series.map((s) => ({ ...s, label: vatLabelMap[s.key] ?? s.label }))}
            ariaLabel={t('ies.result.fin_tax_vat')}
            yLabel={ccy}
            unit={ccy}
            showAllLabel={t('ies.result.show_all_series')}
            hoverHint={t('ies.result.chart_hover_hint')}
          />
        ) : (
          <p className="ies-text-secondary">
            {t('ies.result.fin_tax_none')}
          </p>
        )}
      </Card>

      {/* 3. 所得税、折旧、亏损与派生税费 */}
      <Card
        title={t('ies.result.tax_income_card')}
        actions={
          <div className="ies-form-row">
            {incomeChart.rules.length > 1 ? (
              <Select
                aria-label={t('ies.result.filter_rule')}
                value={incomeChart.activeRule ?? ''}
                onChange={(e) => setSelectedRule(e.target.value)}
              >
                {incomeChart.rules.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </Select>
            ) : null}
            <Button variant="secondary" size="sm" onClick={() => onOpenDetail('income')}>
              {t('ies.result.view_detail_data', { count: incomeTotal })}
            </Button>
          </div>
        }
        className="ies-section"
      >
        <div className="ies-form-row" style={{ marginBottom: 'var(--ies-space-3)' }}>
          <Badge variant="neutral" label={`${t('ies.result.tax_income_tab')}: ${income.length}`} />
          <Badge variant="neutral" label={`${t('ies.result.tax_depreciation_tab')}: ${depreciation.length}`} />
          <Badge variant="neutral" label={t('ies.result.fin_tax_loss_count', { count: lossBalances.length })} />
          <Badge variant="neutral" label={t('ies.result.fin_tax_derived_count', { count: derived.length })} />
        </div>
        <div className="ies-chart-grid">
          <div>
            <h3 className="ies-section-subtitle">
              {t('ies.result.fin_tax_income')}
            </h3>
            {incomeChart.categories.length > 0 ? (
              <BarChart
                categories={incomeChart.categories}
                series={incomeChart.series.map((s) => ({ ...s, label: incomeLabelMap[s.key] ?? s.label }))}
                ariaLabel={t('ies.result.fin_tax_income')}
                yLabel={ccy}
                unit={ccy}
                hoverHint={t('ies.result.chart_hover_hint')}
              />
            ) : (
              <p className="ies-text-secondary">
                {t('ies.result.fin_tax_none')}
              </p>
            )}
          </div>
          <div>
            <h3 className="ies-section-subtitle">
              {t('ies.result.fin_tax_depreciation')}
            </h3>
            {depChart.categories.length > 0 ? (
              <BarChart
                categories={depChart.categories}
                series={depChart.series}
                ariaLabel={t('ies.result.fin_tax_depreciation')}
                yLabel={ccy}
                unit={ccy}
                hoverHint={t('ies.result.chart_hover_hint')}
              />
            ) : (
              <p className="ies-text-secondary">
                {t('ies.result.fin_tax_none')}
              </p>
            )}
            <p className="ies-text-note" style={{ marginTop: 'var(--ies-space-2)' }}>
              {t('ies.result.fin_tax_depreciation_note')}
            </p>
          </div>
        </div>
      </Card>

      {/* 4. 命名成本明细与命名金额 */}
      <Card
        title={t('ies.result.relations_card')}
        actions={
          <Button variant="secondary" size="sm" onClick={() => onOpenDetail('relations')}>
            {t('ies.result.view_detail_data', { count: relationsTotal })}
          </Button>
        }
        className="ies-section"
      >
        <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-3)' }}>
          {t('ies.result.fin_relations_note')}
        </p>
        {costGroups.length === 0 && amountGroups.length === 0 ? (
          <p className="ies-text-secondary">
            {t('ies.result.fin_cost_items_none')}
          </p>
        ) : (
          <div className="ies-chart-grid">
            {costGroups.map((grp) => (
              <div key={`cost-${grp.unit}`}>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.fin_cost_items')} ({grp.unit})
                </h3>
                {grp.truncated ? (
                  <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>
                    {t('ies.result.preview_capped_note', { shown: grp.categories.length, total: grp.totalCount })}
                  </p>
                ) : null}
                <BarChart
                  categories={grp.categories}
                  series={grp.series.map((s) => ({ ...s, label: costLabelMap[s.key] ?? s.label }))}
                  ariaLabel={`${t('ies.result.fin_cost_items')} (${grp.unit})`}
                  yLabel={grp.unit}
                  unit={grp.unit}
                  hoverHint={t('ies.result.chart_hover_hint')}
                />
              </div>
            ))}
            {amountGroups.map((grp) => (
              <div key={`amt-${grp.axis}-${grp.unit}`}>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.fin_amounts')} ({grp.axis} · {grp.unit})
                </h3>
                {grp.axis === 'scalar' ? (
                  <BarChart
                    categories={grp.categories}
                    series={grp.series}
                    ariaLabel={`${t('ies.result.fin_amounts')} (${grp.axis})`}
                    yLabel={grp.unit}
                    unit={grp.unit}
                    hoverHint={t('ies.result.chart_hover_hint')}
                  />
                ) : (
                  <CategoryLineChart
                    categories={grp.categories}
                    series={grp.series}
                    ariaLabel={`${t('ies.result.fin_amounts')} (${grp.axis})`}
                    yLabel={grp.unit}
                    unit={grp.unit}
                    showAllLabel={t('ies.result.show_all_series')}
                    hoverHint={t('ies.result.chart_hover_hint')}
                  />
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 5. 具名收付与资产账面值 */}
      <Card
        title={t('ies.result.transactions_card')}
        actions={
          <Button variant="secondary" size="sm" onClick={() => onOpenDetail('transactions')}>
            {t('ies.result.view_detail_data', { count: transactionsTotal })}
          </Button>
        }
        className="ies-section"
      >
        <div className="ies-form-row" style={{ marginBottom: 'var(--ies-space-2)' }}>
          <Badge variant="neutral" label={t('ies.result.fin_movement_count', { count: cashMovements.length })} />
          <Badge variant="neutral" label={t('ies.result.fin_asset_count', { count: assets.length })} />
        </div>
        <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-3)' }}>
          {t('ies.result.fin_movements_note')}
        </p>
        {assetChart.categories.length > 0 ? (
          <>
            <h3 className="ies-section-subtitle">
              {t('ies.result.fin_assets')}
            </h3>
            <CategoryLineChart
              categories={assetChart.categories}
              series={assetChart.series}
              ariaLabel={t('ies.result.fin_assets')}
              yLabel={ccy}
              unit={ccy}
              showAllLabel={t('ies.result.show_all_series')}
              hoverHint={t('ies.result.chart_hover_hint')}
            />
            <p className="ies-text-note" style={{ marginTop: 'var(--ies-space-2)' }}>
              {t('ies.result.fin_assets_note')}
            </p>
          </>
        ) : cashMovements.length === 0 ? (
          <p className="ies-text-secondary">
            {t('ies.result.fin_tax_none')}
          </p>
        ) : null}
      </Card>
    </>
  )
}
