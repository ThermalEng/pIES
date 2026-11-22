/**
 * 结果页「工程与曲线」Tab（计算步曲线图 + 图例过滤 + 二级原始步序列数据表入口 + 工程/环境/可靠性指标）。
 */

import { LineChart } from '../../components/charts'
import type { LineSeries } from '../../components/charts'
import { Alert, Button, Card, EmptyState, Input } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { Currency, MetricValue } from '../../types'
import type { ResultDetail } from './chartMappers'
import type { HourlySeriesMeta } from './model'
import { findMetric, formatMetricValue, MetricTable, pickMetrics } from './resultHelpers'

export interface HourlyWindowState {
  flows: Record<string, number[]>
  series: HourlySeriesMeta[]
  start: number
  end: number
  totalRows: number
}

export interface EngineeringTabViewProps {
  hourly: HourlyWindowState | null
  hourlyAbsent: boolean
  hourlyError: string | null
  hourlyLoading: boolean
  hourlyJump: string
  onHourlyJumpChange: (value: string) => void
  onRetryHourly: (start?: number) => void
  onOpenDetail: (detail: ResultDetail) => void
  metrics: Record<string, MetricValue> | null
  currency: Currency
}

export function EngineeringTabView({
  hourly,
  hourlyAbsent,
  hourlyError,
  hourlyLoading,
  hourlyJump,
  onHourlyJumpChange,
  onRetryHourly,
  onOpenDetail,
  metrics,
  currency,
}: EngineeringTabViewProps) {
  const { t } = useI18n()

  const palette = [
    'var(--ies-primary)',
    'var(--ies-danger)',
    'var(--ies-warning)',
    'var(--ies-success)',
    'var(--ies-chart-purple)',
    'var(--ies-chart-teal)',
  ]

  const hourlyGroups: { unit: string; series: LineSeries[] }[] = []
  if (hourly) {
    const groups = new Map<string, { unit: string; series: LineSeries[] }>()
    let index = 0
    for (const meta of hourly.series) {
      const values = hourly.flows[meta.series_id]
      if (!values?.length) continue
      const label =
        meta.kind === 'device'
          ? `${meta.instance_id}.${meta.variable_id}`
          : `${t('ies.result.system_series')} ${meta.block}.${meta.variable}`
      let group = groups.get(meta.unit)
      if (!group) {
        group = { unit: meta.unit, series: [] }
        groups.set(meta.unit, group)
      }
      group.series.push({
        key: meta.series_id,
        label,
        color: palette[index % palette.length],
        values,
      })
      index += 1
    }
    hourlyGroups.push(...groups.values())
  }

  let hourlyPointCount = 0
  for (const group of hourlyGroups) {
    for (const s of group.series) {
      if (s.values.length > hourlyPointCount) hourlyPointCount = s.values.length
    }
  }

  const envMetrics = pickMetrics(metrics, [/^env\./, /co2/i, /emission/i, /carbon/i, /factor/i])
  const co2Metric = findMetric(metrics, [/^env\.co2/i, /^co2/i, /co2$/i, /^env\.emission/i])
  const boundaryMetric = findMetric(metrics, [/boundary/i])
  const factorMetric = findMetric(metrics, [/factor/i])
  const envOthers = envMetrics.filter(
    (m) => m !== co2Metric && m !== boundaryMetric && m !== factorMetric,
  )

  const balanceMetrics = pickMetrics(metrics, [/balance|^energy\.|^eng\.balance/i])
  const capacityMetrics = pickMetrics(metrics, [/capacity|^dev\.|^eng\.device/i])
  const peakMetrics = pickMetrics(metrics, [/peak/i])
  const gridMetrics = pickMetrics(metrics, [/grid|p_grid|purchase|sell/i])
  const demandMetrics = pickMetrics(metrics, [/demand/i])
  const relMetrics = pickMetrics(metrics, [/^rel\.|sample/i])

  const hasHourlyBlock = Boolean(hourly || hourlyAbsent || hourlyError || hourlyLoading)
  const hasEng =
    balanceMetrics.length > 0 ||
    capacityMetrics.length > 0 ||
    peakMetrics.length > 0 ||
    gridMetrics.length > 0 ||
    demandMetrics.length > 0
  const hasEnv = envMetrics.length > 0
  const hasRel = relMetrics.length > 0

  if (!hasHourlyBlock && !hasEng && !hasEnv && !hasRel) {
    return (
      <Card title={t('ies.result.tab_engineering')} className="ies-section">
        <EmptyState icon="info" title={t('ies.result.no_engineering_data')} />
      </Card>
    )
  }

  return (
    <>
      {hasHourlyBlock ? (
        hourlyGroups.length > 0 ? (
          <Card
            title={t('ies.result.hourly_chart')}
            actions={
              <Button variant="secondary" size="sm" onClick={() => onOpenDetail('hourly')}>
                {t('ies.result.hourly_table_btn')}
              </Button>
            }
            className="ies-section"
          >
            <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>
              {t('ies.result.hourly_note', {
                n: hourlyPointCount,
                start: hourly?.start ?? 0,
                end: (hourly?.end ?? 1) - 1,
                total: hourly?.totalRows ?? 0,
              })}
            </p>
            {hourly ? (
              <div className="ies-form-row" style={{ marginBottom: 'var(--ies-space-2)' }}>
                <Button
                  disabled={hourlyLoading || hourly.start === 0}
                  onClick={() => onRetryHourly(Math.max(0, hourly.start - 1000))}
                >
                  {t('ies.result.hourly_previous')}
                </Button>
                <Button
                  disabled={hourlyLoading || hourly.end >= hourly.totalRows}
                  onClick={() => onRetryHourly(hourly.end)}
                >
                  {t('ies.result.hourly_next')}
                </Button>
                <label className="ies-inline-field">
                  <span>{t('ies.result.hourly_start')}</span>
                  <Input
                    type="number"
                    min={0}
                    max={hourly.totalRows - 1}
                    step={1}
                    value={hourlyJump}
                    onChange={(e) => onHourlyJumpChange(e.target.value)}
                  />
                </label>
                <Button
                  disabled={
                    hourlyLoading ||
                    hourlyJump === '' ||
                    !Number.isInteger(Number(hourlyJump)) ||
                    Number(hourlyJump) < 0 ||
                    Number(hourlyJump) >= hourly.totalRows
                  }
                  onClick={() => onRetryHourly(Number(hourlyJump))}
                >
                  {t('ies.result.hourly_go')}
                </Button>
                {hourlyLoading ? <span role="status">{t('ies.common.loading')}</span> : null}
                {hourlyError ? <Alert variant="error">{hourlyError}</Alert> : null}
              </div>
            ) : null}
            {hourlyGroups.map((group) => (
              <div className="ies-section" key={group.unit}>
                <LineChart
                  series={group.series}
                  ariaLabel={`${t('ies.result.hourly_chart')} (${group.unit})`}
                  yLabel={group.unit}
                  xLabel={t('ies.result.hourly_step_axis')}
                  xOffset={hourly?.start ?? 0}
                  toggleable
                  showAllLabel={t('ies.result.show_all_series')}
                />
              </div>
            ))}
          </Card>
        ) : (
          <Card title={t('ies.result.hourly_chart')} className="ies-section">
            {hourlyError ? (
              <>
                <Alert variant="error">{hourlyError}</Alert>
                <p className="ies-text-secondary" style={{ marginTop: 'var(--ies-space-2)' }}>
                  {t('ies.result.hourly_failed')}
                </p>
                <div style={{ marginTop: 'var(--ies-space-2)' }}>
                  <Button onClick={() => onRetryHourly()} disabled={hourlyLoading}>
                    {t('ies.common.retry')}
                  </Button>
                </div>
              </>
            ) : hourlyAbsent || (hourly && !hourlyLoading) ? (
              <p className="ies-text-secondary">
                {t('ies.result.hourly_absent')}
              </p>
            ) : (
              <p className="ies-text-secondary">
                {t('ies.result.hourly_loading')}
              </p>
            )}
          </Card>
        )
      ) : null}

      {hasEng ? (
        <Card title={t('ies.result.eng_card')} className="ies-section">
          {balanceMetrics.length > 0 ? (
            <div style={{ marginBottom: 'var(--ies-space-3)' }}>
              <h3 className="ies-section-subtitle">
                {t('ies.result.energy_balance_table')}
              </h3>
              <MetricTable metrics={balanceMetrics} currency={currency} />
            </div>
          ) : null}
          <div className="ies-grid ies-grid--cols-3">
            {capacityMetrics.length > 0 ? (
              <div className="ies-section">
                <h3 className="ies-section-subtitle">
                  {t('ies.result.device_capacity')}
                </h3>
                <MetricTable metrics={capacityMetrics} currency={currency} />
              </div>
            ) : null}
            {peakMetrics.length > 0 ? (
              <div className="ies-section">
                <h3 className="ies-section-subtitle">
                  {t('ies.result.peak')}
                </h3>
                <MetricTable metrics={peakMetrics} currency={currency} />
              </div>
            ) : null}
            {gridMetrics.length > 0 ? (
              <div className="ies-section">
                <h3 className="ies-section-subtitle">
                  {t('ies.result.grid_purchase_sale')}
                </h3>
                <MetricTable metrics={gridMetrics} currency={currency} />
              </div>
            ) : null}
          </div>
          {demandMetrics.length > 0 ? (
            <div>
              <h3 className="ies-section-subtitle">
                {t('ies.result.demand_charge')}
              </h3>
              <MetricTable metrics={demandMetrics} currency={currency} />
            </div>
          ) : null}
        </Card>
      ) : null}

      {hasEnv ? (
        <Card title={t('ies.result.env_card')} className="ies-section">
          <dl className="ies-kv">
            <dt>{t('ies.result.co2')}</dt>
            <dd>{co2Metric ? formatMetricValue(co2Metric, currency) : '—'}</dd>
            <dt>{t('ies.result.emission_boundary')}</dt>
            <dd>{boundaryMetric ? formatMetricValue(boundaryMetric, currency) : '—'}</dd>
            <dt>{t('ies.result.factor_version')}</dt>
            <dd>{factorMetric ? formatMetricValue(factorMetric, currency) : '—'}</dd>
          </dl>
          {envOthers.length > 0 ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <MetricTable metrics={envOthers} currency={currency} />
            </div>
          ) : null}
        </Card>
      ) : null}

      {hasRel ? (
        <Card title={t('ies.result.dimension_reliability')} className="ies-section">
          <MetricTable metrics={relMetrics} currency={currency} />
          <p className="ies-text-note" style={{ marginTop: 'var(--ies-space-2)' }}>
            {t('ies.result.rel_note')}
          </p>
        </Card>
      ) : null}
    </>
  )
}
