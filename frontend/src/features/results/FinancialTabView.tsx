/**
 * 结果页「财务与现金流」Tab（一级可视化：核心口径 + 现金流/运营/付款/比较图表 + 二级钻取入口）。
 */

import { BarChart, CategoryLineChart, ComboChart } from '../../components/charts'
import { Button, Card } from '../../components/ui'
import { useI18n } from '../../i18n'
import type {
  Currency,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  TaskOutcome,
} from '../../types'
import {
  buildAnnualOperationChartData,
  buildCashflowChartData,
  buildComparisonChartData,
} from './chartMappers'
import type { ResultDetail } from './chartMappers'
import {
  FinancialAbsenceAlert,
  irrStatusToKey,
  MetricGroup,
  MetricTable,
  pickMetrics,
  SealedFinancialCore,
} from './resultHelpers'

export interface FinancialTabViewProps {
  fin: FinancialEvaluationBlock | null
  finAbsence: FinancialAbsence
  outcome: TaskOutcome | null
  metrics: Record<string, MetricValue> | null
  currency: Currency
  onOpenDetail: (detail: ResultDetail) => void
}

export function FinancialTabView({
  fin,
  finAbsence,
  outcome,
  metrics,
  currency,
  onOpenDetail,
}: FinancialTabViewProps) {
  const { t } = useI18n()

  const finMetrics = pickMetrics(metrics, [
    /^fin\./,
    /irr/i,
    /cashflow/i,
    /investment/i,
    /revenue/i,
    /opex|o&m|running.?cost/i,
  ])
  const cashflowMetrics = pickMetrics(metrics, [/cashflow|cf_/i])
  const irrKey = fin?.evaluation?.irr_status ? irrStatusToKey(fin.evaluation.irr_status) : null

  const cfChart = fin ? buildCashflowChartData(fin) : null
  const cfLabelMap: Record<string, string> = {
    net: t('ies.result.cf_net'),
    discounted: t('ies.result.cf_discounted'),
    cumulative: t('ies.result.cf_cumulative'),
    cumulative_discounted: t('ies.result.cf_cumulative_discounted'),
  }

  const opChart = fin ? buildAnnualOperationChartData(fin) : null
  const opLabelMap: Record<string, string> = {
    energy_net: t('ies.result.op_energy_net'),
    variable_om: t('ies.result.op_variable_om'),
    fixed_om: t('ies.result.op_fixed_om'),
    maintenance: t('ies.result.op_maintenance'),
    demand_charge: t('ies.result.op_demand_charge'),
    net: t('ies.result.op_net'),
  }

  const cmpChart = fin ? buildComparisonChartData(fin) : null
  const cmpLabelMap: Record<string, string> = {
    candidate: t('ies.result.comparison_candidate'),
    baseline: t('ies.result.comparison_baseline'),
    difference: t('ies.result.comparison_diff'),
  }

  const cfRowCount = fin
    ? Math.max(fin.evaluation?.rows?.length ?? 0, fin.annual_net_flows?.length ?? 0)
    : 0

  return (
    <>
      {fin ? (
        <>
          <Card title={t('ies.result.fin_sealed_title')} className="ies-section">
            <SealedFinancialCore fin={fin} />
          </Card>

          {cfChart && cfChart.categories.length > 0 ? (
            <Card
              title={
                (fin.evaluation?.rows?.length ?? 0) > 0
                  ? t('ies.result.fin_cf_table')
                  : t('ies.result.fin_tax_flows')
              }
              actions={
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => onOpenDetail('cashflows')}
                >
                  {t('ies.result.view_detail_data', { count: cfRowCount })}
                </Button>
              }
              className="ies-section"
            >
              <ComboChart
                categories={cfChart.categories}
                bars={cfChart.bars.map((b) => ({ ...b, label: cfLabelMap[b.key] ?? b.label }))}
                lines={cfChart.lines.map((l) => ({ ...l, label: cfLabelMap[l.key] ?? l.label }))}
                ariaLabel={t('ies.result.fin_cf_table')}
                yLabel={fin.currency}
                unit={fin.currency}
                hoverHint={t('ies.result.chart_hover_hint')}
              />
            </Card>
          ) : null}

          {opChart && opChart.categories.length > 0 ? (
            <Card
              title={t('ies.result.fin_yearly')}
              actions={
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => onOpenDetail('operation')}
                >
                  {t('ies.result.view_detail_data', { count: fin.annual_operation.length })}
                </Button>
              }
              className="ies-section"
            >
              <BarChart
                categories={opChart.categories}
                series={opChart.series.map((s) => ({ ...s, label: opLabelMap[s.key] ?? s.label }))}
                ariaLabel={t('ies.result.fin_yearly')}
                yLabel={fin.currency}
                unit={fin.currency}
                hoverHint={t('ies.result.chart_hover_hint')}
              />
            </Card>
          ) : null}

          <Card title={t('ies.result.fin_payments_and_net')} className="ies-section">
            <div className="ies-grid ies-grid--cols-2">
              <div>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.fin_payments')}
                </h3>
                {fin.payments_applied.length > 0 ? (
                  <table className="ies-metric-table">
                    <tbody>
                      {fin.payments_applied.map((p, index) => (
                        <tr key={`${p.t}-${index}`}>
                          <td>{t('ies.result.fin_t_payment', { year: p.t })}</td>
                          <td>
                            {p.ratio} × C = {p.amount} {fin.currency}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="ies-text-secondary">
                    {t('ies.result.fin_tax_none')}
                  </p>
                )}
              </div>
              <div>
                <h3 className="ies-section-subtitle">
                  {t('ies.result.fin_net_cashflows')}
                </h3>
                {fin.net_cashflows.length > 0 ? (
                  <table className="ies-metric-table">
                    <tbody>
                      {fin.net_cashflows.map((item) => (
                        <tr key={`${item.t}-${item.id}`}>
                          <td>
                            {item.id} / CF[t={item.t}] / {t(`ies.result.fin_net_${item.direction}`)}
                          </td>
                          <td>
                            {item.amount} {fin.currency}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="ies-text-secondary">
                    {t('ies.result.fin_net_none')}
                  </p>
                )}
              </div>
            </div>
          </Card>

          <Card
            title={t('ies.result.comparison')}
            actions={
              fin.comparison.mode !== 'absolute' ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => onOpenDetail('comparison')}
                >
                  {t('ies.result.view_detail_generic')}
                </Button>
              ) : undefined
            }
            className="ies-section"
          >
            <p className="ies-text-secondary" style={{ marginBottom: 'var(--ies-space-2)' }}>
              {fin.comparison.mode}
            </p>
            {fin.comparison.mode !== 'absolute' ? (
              <>
                <p className="ies-text-note">
                  {fin.comparison.comparability.service_basis}
                </p>
                <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-3)' }}>
                  {fin.comparison.comparability.differences}
                </p>
                {cmpChart && cmpChart.categories.length > 0 ? (
                  <CategoryLineChart
                    categories={cmpChart.categories}
                    series={cmpChart.series.map((s) => ({ ...s, label: cmpLabelMap[s.key] ?? s.label }))}
                    ariaLabel={t('ies.result.comparison')}
                    yLabel={fin.currency}
                    unit={fin.currency}
                    showAllLabel={t('ies.result.show_all_series')}
                    hoverHint={t('ies.result.chart_hover_hint')}
                  />
                ) : null}
              </>
            ) : null}
          </Card>
        </>
      ) : finAbsence ? (
        <FinancialAbsenceAlert finAbsence={finAbsence} outcome={outcome} />
      ) : null}

      {metrics && (finMetrics.length > 0 || cashflowMetrics.length > 0) ? (
        <Card title={t('ies.result.financial_card')} className="ies-section">
          <MetricGroup
            metrics={finMetrics}
            currency={currency}
            labels={{
              irr: t('ies.result.irr'),
              irrStatus: t('ies.result.irr_status'),
            }}
            irrKey={irrKey}
          />
          {cashflowMetrics.length > 0 ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <h3 className="ies-section-subtitle">
                {t('ies.result.cashflow')}
              </h3>
              <MetricTable metrics={cashflowMetrics} currency={currency} />
            </div>
          ) : null}
        </Card>
      ) : null}
    </>
  )
}
