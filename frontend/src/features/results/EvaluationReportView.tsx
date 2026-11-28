/**
 * 自描述第三方评价器报告块（ies.evaluation-report@1.0.0）通用渲染组件。
 * 严格消费后端/插件声明的 metrics / charts / tables，不重算业务指标。
 */

import { BarChart, CategoryLineChart, ScatterChart } from '../../components/charts'
import type { CategorySeries, ScatterPoint } from '../../components/charts'
import { Alert, Table, TBody, TD, TH, THead, TR } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { EvaluationReportBlock } from '../../types'
import { asObjectArray } from './chartMappers'

const REPORT_COLORS = [
  'var(--ies-primary)',
  'var(--ies-success)',
  'var(--ies-warning)',
  'var(--ies-chart-purple)',
  'var(--ies-chart-teal)',
  'var(--ies-danger)',
]

interface RawReportChart {
  id: string
  title: string
  x_label: string
  y_label: string
  y_unit: string
  x_unit?: string | null
  chart_type?: 'line' | 'bar' | 'area' | 'scatter'
  x_values?: Array<string | number>
  series: Array<{
    id: string
    label: string
    unit?: string | null
    values: Array<number | null>
  }>
}

interface RawReportTable {
  id: string
  title: string
  columns: Array<{ id: string; label: string; unit?: string | null }>
  rows: Array<Array<string | number | boolean | null>>
}

function formatCell(val: unknown): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'object') return JSON.stringify(val)
  return String(val)
}

export function EvaluationReportSection({ reports }: { reports: EvaluationReportBlock[] }) {
  const { t } = useI18n()
  const validReports = asObjectArray<EvaluationReportBlock>(reports)
  if (validReports.length === 0) return null

  return (
    <div className="ies-stack">
      {validReports.map((rep, repIdx) => {
        const repMetrics = asObjectArray<Record<string, unknown>>(rep.metrics)
        const repCharts = asObjectArray<RawReportChart>(rep.charts)
        const repTables = asObjectArray<RawReportTable>(rep.tables)
        const summaryText = typeof rep.summary === 'string' && rep.summary ? rep.summary : null

        return (
          <div
            key={`${String(rep.evaluator ?? repIdx)}-${String(rep.title ?? repIdx)}`}
            className="ies-stack"
            data-eval-report={rep.evaluator}
          >
            <div>
              <h3 className="ies-section-subtitle">
                {rep.title || rep.evaluator}{' '}
                <span className="ies-text-note">
                  ({rep.evaluator} · {rep.schema}@{rep.schema_version})
                </span>
              </h3>
              {summaryText ? (
                <p className="ies-text-secondary" style={{ marginTop: 'var(--ies-space-1)' }}>
                  {summaryText}
                </p>
              ) : null}
            </div>

            {repMetrics.length > 0 ? (
              <dl className="ies-kv">
                {repMetrics.map((m, idx) => {
                  const rawLabel = m.label ?? m.id ?? idx
                  const labelText =
                    typeof rawLabel === 'object' && rawLabel !== null
                      ? JSON.stringify(rawLabel)
                      : String(rawLabel)
                  return (
                    <span key={`${idx}-${String(m.id ?? '')}`} style={{ display: 'contents' }}>
                      <dt>{labelText}</dt>
                      <dd>
                        {formatCell(m.value)}
                        {m.unit ? ` ${String(m.unit)}` : ''}
                      </dd>
                    </span>
                  )
                })}
              </dl>
            ) : null}

            {repCharts.map((chart, cIdx) => {
              const rawSeries = asObjectArray<RawReportChart['series'][number]>(chart.series)
              if (rawSeries.length === 0) return null
              const maxLen = Math.max(
                Array.isArray(chart.x_values) ? chart.x_values.length : 0,
                ...rawSeries.map((s) => (Array.isArray(s.values) ? s.values.length : 0)),
              )
              if (maxLen === 0) return null
              const categories =
                Array.isArray(chart.x_values) && chart.x_values.length > 0
                  ? chart.x_values.map((v) => String(v))
                  : Array.from({ length: maxLen }, (_, i) => String(i))
              const series: CategorySeries[] = rawSeries.map((s, sIdx) => {
                const vals = Array.isArray(s.values)
                  ? s.values.map((v) => (typeof v === 'number' && Number.isFinite(v) ? v : null))
                  : []
                return {
                  key: String(s.id ?? sIdx),
                  label: s.unit ? `${s.label ?? s.id} (${s.unit})` : String(s.label ?? s.id),
                  color: REPORT_COLORS[sIdx % REPORT_COLORS.length],
                  values: vals,
                  rawValues: vals.map((v) => (v === null ? null : String(v))),
                }
              })
              const xAxisLabel = chart.x_unit ? `${chart.x_label} (${chart.x_unit})` : chart.x_label
              const yAxisLabel = chart.y_unit ? `${chart.y_label} (${chart.y_unit})` : chart.y_label
              const chartType = chart.chart_type ?? 'line'

              if (chartType === 'scatter') {
                const points: ScatterPoint[] = []
                for (let sIdx = 0; sIdx < rawSeries.length; sIdx += 1) {
                  const s = rawSeries[sIdx]
                  const vals = Array.isArray(s.values) ? s.values : []
                  for (let i = 0; i < vals.length; i += 1) {
                    const yVal = vals[i]
                    if (typeof yVal !== 'number' || !Number.isFinite(yVal)) continue
                    const rawX = Array.isArray(chart.x_values) ? chart.x_values[i] : i
                    const parsedX = typeof rawX === 'number' ? rawX : Number(rawX)
                    const xVal = Number.isFinite(parsedX) ? parsedX : i
                    points.push({
                      id: `${String(s.id ?? sIdx)}-${i}`,
                      x: xVal,
                      y: yVal,
                      label: `${String(s.label ?? s.id)} (${xVal}, ${yVal})`,
                    })
                  }
                }
                return (
                  <div key={`${chart.id ?? cIdx}`} className="ies-section">
                    <h4 className="ies-section-subtitle">{chart.title}</h4>
                    <ScatterChart
                      points={points}
                      ariaLabel={chart.title}
                      xLabel={xAxisLabel}
                      yLabel={yAxisLabel}
                    />
                  </div>
                )
              }

              if (chartType !== 'bar' && chartType !== 'line' && chartType !== 'area') {
                return (
                  <div key={`${chart.id ?? cIdx}`} className="ies-section">
                    <h4 className="ies-section-subtitle">{chart.title}</h4>
                    <Alert variant="info" title={`Unsupported chart_type: ${String(chartType)}`} />
                  </div>
                )
              }

              return (
                <div key={`${chart.id ?? cIdx}`} className="ies-section">
                  <h4 className="ies-section-subtitle">{chart.title}</h4>
                  {chartType === 'bar' ? (
                    <BarChart
                      categories={categories}
                      series={series}
                      ariaLabel={chart.title}
                      xLabel={xAxisLabel}
                      yLabel={yAxisLabel}
                      unit={chart.y_unit}
                      hoverHint={t('ies.result.chart_hover_hint')}
                    />
                  ) : (
                    <CategoryLineChart
                      categories={categories}
                      series={series}
                      ariaLabel={chart.title}
                      xLabel={xAxisLabel}
                      yLabel={yAxisLabel}
                      unit={chart.y_unit}
                      fillArea={chartType === 'area'}
                      showAllLabel={t('ies.result.show_all_series')}
                      hoverHint={t('ies.result.chart_hover_hint')}
                    />
                  )}
                </div>
              )
            })}

            {repTables.map((tbl, tIdx) => {
              const cols = asObjectArray<RawReportTable['columns'][number]>(tbl.columns)
              const rows = Array.isArray(tbl.rows) ? tbl.rows : []
              if (cols.length === 0) return null
              return (
                <div key={`${tbl.id ?? tIdx}`} className="ies-section">
                  <h4 className="ies-section-subtitle">{tbl.title}</h4>
                  <Table aria-label={tbl.title}>
                    <THead>
                      <TR>
                        {cols.map((col, colIdx) => (
                          <TH key={col.id ?? colIdx}>
                            {col.label}
                            {col.unit ? ` (${col.unit})` : ''}
                          </TH>
                        ))}
                      </TR>
                    </THead>
                    <TBody>
                      {rows.map((row, rIdx) => {
                        const cells = Array.isArray(row) ? row : []
                        return (
                          <TR key={rIdx}>
                            {cols.map((col, colIdx) => (
                              <TD key={`${rIdx}-${col.id ?? colIdx}`}>{formatCell(cells[colIdx])}</TD>
                            ))}
                          </TR>
                        )
                      })}
                    </TBody>
                  </Table>
                </div>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}
