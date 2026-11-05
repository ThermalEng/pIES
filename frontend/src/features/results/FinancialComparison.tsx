import { useI18n } from '../../i18n'
import type { FinancialEvaluationBlock } from '../../types'

/** Read sealed candidate/baseline/evaluation values; never subtract or retax here. */
export function FinancialComparison({ fin }: { fin: FinancialEvaluationBlock }) {
  const { t } = useI18n()
  const comparison = fin.comparison
  return <section aria-label={t('ies.result.comparison')}>
    <h3>{t('ies.result.comparison')}</h3>
    <p>{comparison.mode}</p>
    {comparison.mode !== 'absolute' && <>
      <p>{comparison.comparability.service_basis}</p>
      <p>{comparison.comparability.differences}</p>
      <pre className="ies-pre">{JSON.stringify(comparison.baseline_source, null, 2)}</pre>
      <div style={{ overflowX: 'auto' }}>
        <table className="ies-metric-table" aria-label={t('ies.result.comparison')}>
          <thead><tr><th>t</th><th>{t('ies.result.candidate_cash')}</th>
            {comparison.mode === 'difference' && <><th>{t('ies.result.baseline_cash')}</th><th>{t('ies.result.difference_cash')}</th></>}
          </tr></thead>
          <tbody>{fin.annual_net_flows.map((value, index) => <tr key={index}>
            <td>{index}</td><td>{value} {fin.currency}</td>
            {comparison.mode === 'difference' && <><td>{comparison.baseline_net_flows[index]} {fin.currency}</td>
              <td>{comparison.evaluation_net_flows[index]} {fin.currency}</td></>}
          </tr>)}</tbody>
        </table>
      </div>
      <details><summary>facts</summary><pre className="ies-pre">{JSON.stringify(comparison.facts, null, 2)}</pre></details>
    </>}
  </section>
}
