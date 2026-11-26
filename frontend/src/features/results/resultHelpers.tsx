/**
 * 结果分析页共享只读展示组件与纯辅助函数（不重算业务值）。
 */

import { Alert, Badge } from '../../components/ui'
import { translate, useI18n } from '../../i18n'
import { formatCo2, formatMoney, formatNumber, formatPercent } from '../../lib/format'
import type {
  Currency,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  TaskOutcome,
} from '../../types'
import { paginateItems } from './chartMappers'

export type IrrStatusKey =
  | 'unique'
  | 'no_solution'
  | 'multiple'
  | 'degenerate'
  | 'out_of_domain'
  | 'numeric_failure'

export const DEFAULT_PAGE_SIZE = 20

/** 封存 evaluation.irr_status → 徽章键(唯一权威映射, 摘要与封存卡共用)。 */
export function irrStatusToKey(irrStatus: string): IrrStatusKey | null {
  return irrStatus === 'normal'
    ? 'unique'
    : irrStatus === 'no_solution' ||
        irrStatus === 'multiple' ||
        irrStatus === 'degenerate' ||
        irrStatus === 'out_of_domain' ||
        irrStatus === 'numeric_failure'
      ? (irrStatus as IrrStatusKey)
      : null
}

export function pickMetrics(
  metrics: Record<string, MetricValue> | null,
  patterns: RegExp[],
): MetricValue[] {
  if (!metrics) return []
  return Object.entries(metrics)
    .filter(([id]) => patterns.some((re) => re.test(id)))
    .map(([, m]) => m)
}

export function findMetric(
  metrics: Record<string, MetricValue> | null,
  patterns: RegExp[],
): MetricValue | null {
  if (!metrics) return null
  const hit = Object.entries(metrics).find(([id]) => patterns.some((re) => re.test(id)))
  return hit ? hit[1] : null
}

/** 指标数值格式化(金额/CO2/百分比特殊处理,其余按数值 + 单位)。 */
export function formatMetricValue(m: MetricValue, currency: Currency): string {
  if (m.value === null || m.value === undefined || Number.isNaN(m.value)) return '—'
  const unit = (m.unit ?? '').toLowerCase()
  if (unit.includes('cny') || unit.includes('usd') || unit.includes('¥') || unit.includes('$')) {
    return formatMoney(m.value, currency)
  }
  if (unit.includes('co2') || unit.includes('co₂') || unit.includes('tco2')) {
    return formatCo2(m.value)
  }
  if (unit === '%' || unit === 'percent' || unit === 'pct') {
    return formatPercent(m.value / 100)
  }
  return `${formatNumber(m.value, { digits: 2 })}${m.unit ? ` ${m.unit}` : ''}`
}

/** 指标表(名称 / 数值)。 */
export function MetricTable({ metrics, currency }: { metrics: MetricValue[]; currency: Currency }) {
  return (
    <table className="ies-metric-table">
      <tbody>
        {metrics.map((m) => (
          <tr key={m.id}>
            <td>{translate(m.label_key)}</td>
            <td>{formatMetricValue(m, currency)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 财务指标组:IRR 状态 + 关键指标。 */
export function MetricGroup({
  metrics,
  currency,
  labels,
  irrKey,
}: {
  metrics: MetricValue[]
  currency: Currency
  labels: { irr: string; irrStatus: string }
  irrKey: IrrStatusKey | null
}) {
  const { t } = useI18n()
  const irrMetric = metrics.find((m) => /irr/i.test(m.id) && !/status/i.test(m.id)) ?? null
  const others = metrics.filter((m) => m !== irrMetric)
  return (
    <>
      <dl className="ies-kv">
        <dt>{labels.irr}</dt>
        <dd>{irrMetric ? formatMetricValue(irrMetric, currency) : '—'}</dd>
        {irrKey ? (
          <>
            <dt>{labels.irrStatus}</dt>
            <dd>
              <Badge
                variant={irrKey === 'unique' ? 'success' : 'warning'}
                icon={irrKey === 'unique' ? 'check' : 'warning'}
                label={t(`ies.result.irr_${irrKey}`)}
              />
            </dd>
          </>
        ) : null}
      </dl>
      {others.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <MetricTable metrics={others} currency={currency} />
        </div>
      ) : null}
    </>
  )
}

/** 已封存财务评价头与核心四指标（保持 direct <dt>/<dd> DOM 结构）。 */
export function SealedFinancialCore({ fin }: { fin: FinancialEvaluationBlock }) {
  const { t } = useI18n()
  const ev = fin?.evaluation
  if (!ev) return null
  const irrKey = irrStatusToKey(ev.irr_status)
  const staticPb = { value: ev.static_payback, status: ev.static_status }
  const dynamicPb = { value: ev.dynamic_payback, status: ev.dynamic_status }

  return (
    <>
      <dl className="ies-kv">
        <dt>{t('ies.result.fin_e')}</dt>
        <dd>{fin.evaluation_years}</dd>
        <dt>{t('ies.result.fin_k')}</dt>
        <dd>{fin.construction_period}</dd>
        <dt>{t('ies.result.fin_n')}</dt>
        <dd>{fin.operation_years}</dd>
        <dt>{t('ies.result.fin_construction_total')}</dt>
        <dd>
          {fin.construction_total} {fin.construction_total_unit}
        </dd>
        <dt>{t('ies.result.fin_discount')}</dt>
        <dd>{fin.discount_rate}</dd>
        <dt>{t('ies.result.fin_basis')}</dt>
        <dd>
          {fin.price_basis} / {t('ies.result.fin_base_year')}: {fin.base_year}
        </dd>
      </dl>
      <p className="ies-text-secondary" style={{ marginTop: 'var(--ies-space-2)' }}>
        {t('ies.result.fin_axis_note')}
      </p>
      <div style={{ marginTop: 'var(--ies-space-3)' }}>
        <h3 className="ies-section-subtitle">
          {t('ies.result.fin_metrics')}
        </h3>
        <dl className="ies-kv">
          <dt>NPV</dt>
          <dd>
            {ev.npv} {ev.npv_unit}
          </dd>
          <dt>{t('ies.result.irr')}</dt>
          <dd>{ev.irr ?? '—'}</dd>
          <dt>{t('ies.result.irr_status')}</dt>
          <dd>
            {irrKey ? (
              <Badge
                variant={irrKey === 'unique' ? 'success' : 'warning'}
                icon={irrKey === 'unique' ? 'check' : 'warning'}
                label={t(`ies.result.irr_${irrKey}`)}
              />
            ) : (
              '—'
            )}
          </dd>
          <dt>{t('ies.result.fin_payback_static')}</dt>
          <dd>
            {staticPb.value ?? '—'}{' '}
            {staticPb.value !== null && staticPb.value !== undefined ? t('ies.result.fin_year_unit') : ''}{' '}
            {staticPb.status ? t(`ies.result.fin_pb_${staticPb.status}`) : ''}
          </dd>
          <dt>{t('ies.result.fin_payback_dynamic')}</dt>
          <dd>
            {dynamicPb.value ?? '—'}{' '}
            {dynamicPb.value !== null && dynamicPb.value !== undefined ? t('ies.result.fin_year_unit') : ''}{' '}
            {dynamicPb.status ? t(`ies.result.fin_pb_${dynamicPb.status}`) : ''}
          </dd>
        </dl>
      </div>
    </>
  )
}

/** 财务评价缺块统一提示组件(供总览/财务/税务共享, 不重算、不猜测)。 */
export function FinancialAbsenceAlert({
  finAbsence,
  outcome,
}: {
  finAbsence: FinancialAbsence
  outcome: TaskOutcome | null
}) {
  const { t } = useI18n()
  if (!finAbsence) return null
  const text =
    finAbsence === 'outcome_not_normal'
      ? t('ies.result.fin_absence_outcome_not_normal', { outcome: outcome ?? '—' })
      : finAbsence === 'evaluators_empty'
        ? t('ies.result.fin_absence_evaluators_empty')
        : finAbsence === 'evaluator_failed'
          ? t('ies.result.fin_absence_evaluator_failed')
          : finAbsence === 'solution_not_requested'
            ? t('ies.result.fin_absence_solution_not_requested')
            : t('ies.result.fin_absence_missing_unknown')
  return (
    <div className="ies-section">
      <Alert variant={finAbsence === 'evaluator_failed' ? 'error' : 'warning'}>{text}</Alert>
    </div>
  )
}

/** 格式化评价器错误诊断详情(兼容字符串与结构化对象，避免 React 渲染对象崩溃)。 */
export function formatEvaluatorDetail(detail: unknown): string | null {
  if (detail === undefined || detail === null || detail === '') return null
  return typeof detail === 'string' ? detail : JSON.stringify(detail)
}

/** 通用静态数组分页辅助，确保所有组件共用一致默认页大小与边界裁剪。 */
export function slicePage<T>(items: readonly T[], page: number, pageSize = DEFAULT_PAGE_SIZE) {
  return paginateItems(items, page, pageSize)
}
