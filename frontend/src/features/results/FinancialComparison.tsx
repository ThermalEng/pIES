import { useEffect, useState } from 'react'

import { Pagination } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { FinancialEvaluationBlock } from '../../types'
import { paginateItems } from './chartMappers'

export interface FinancialComparisonProps {
  fin: FinancialEvaluationBlock
  /** 启用表格分页；默认 false 以保持静态渲染单测兼容。 */
  paginated?: boolean
  defaultPageSize?: number
}

/** Read sealed candidate/baseline/evaluation values; never subtract or retax here. */
export function FinancialComparison({
  fin,
  paginated = false,
  defaultPageSize = 20,
}: FinancialComparisonProps) {
  const { t } = useI18n()
  const comparison = fin.comparison
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(defaultPageSize)

  useEffect(() => {
    setPage(1)
  }, [fin.annual_net_flows])

  const rowIndices = fin.annual_net_flows.map((_, i) => i)
  const slice = paginated
    ? paginateItems(rowIndices, page, pageSize)
    : { items: rowIndices, page: 1, pageSize: rowIndices.length || 1, total: rowIndices.length, totalPages: 1 }

  return (
    <section aria-label={t('ies.result.comparison')}>
      <h3>{t('ies.result.comparison')}</h3>
      <p>{comparison.mode}</p>
      {comparison.mode !== 'absolute' ? (
        <>
          <p>{comparison.comparability.service_basis}</p>
          <p>{comparison.comparability.differences}</p>
          <pre className="ies-pre">{JSON.stringify(comparison.baseline_source, null, 2)}</pre>
          <div style={{ overflowX: 'auto' }}>
            <table className="ies-metric-table" aria-label={t('ies.result.comparison')}>
              <thead>
                <tr>
                  <th>t</th>
                  <th>{t('ies.result.candidate_cash')}</th>
                  {comparison.mode === 'difference' ? (
                    <>
                      <th>{t('ies.result.baseline_cash')}</th>
                      <th>{t('ies.result.difference_cash')}</th>
                    </>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {slice.items.map((index) => (
                  <tr key={index}>
                    <td>{index}</td>
                    <td>{fin.annual_net_flows[index]} {fin.currency}</td>
                    {comparison.mode === 'difference' ? (
                      <>
                        <td>{comparison.baseline_net_flows[index]} {fin.currency}</td>
                        <td>{comparison.evaluation_net_flows[index]} {fin.currency}</td>
                      </>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {paginated ? (
            <Pagination
              page={slice.page}
              pageSize={slice.pageSize}
              total={slice.total}
              onPageChange={setPage}
              onPageSizeChange={(next) => {
                setPageSize(next)
                setPage(1)
              }}
            />
          ) : null}
          <details style={{ marginTop: 'var(--ies-space-2)' }}>
            <summary>facts</summary>
            <pre className="ies-pre">{JSON.stringify(comparison.facts, null, 2)}</pre>
          </details>
        </>
      ) : null}
    </section>
  )
}
