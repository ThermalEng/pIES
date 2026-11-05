import { useI18n } from '../../i18n'
import type { FinancialEvaluationBlock, FinancialTimePoint } from '../../types'

function timePoint(point: FinancialTimePoint): string {
  return point.kind === 'financial_year' ? `financial_year=${point.year}` : `tax_month=${point.month}`
}

/** Occurrence, payment, asset and tax facts stay distinct; no totals are rebuilt. */
export function FinancialTransactions({ fin }: { fin: FinancialEvaluationBlock }) {
  const { t } = useI18n()
  const ccy = fin.currency
  const none = t('ies.result.fin_tax_none')
  return <>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_movements')}</h3>
      <p>{t('ies.result.fin_movements_note')}</p>
      {fin.cash_movements.length === 0 ? <p>{none}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_movements')}>
            <thead><tr>
              <th>{t('ies.result.fin_movement_identity')}</th>
              <th>{t('ies.result.fin_movement_direction_basis')}</th>
              <th>{t('ies.result.fin_relation_raw')}</th>
              <th>{t('ies.result.fin_relation_gross')}</th>
              <th>{t('ies.result.fin_relation_deductible')}</th>
              <th>{t('ies.result.fin_relation_net_basis')}</th>
              <th>{t('ies.result.fin_output_tax')}</th>
              <th>{t('ies.result.fin_net_revenue')}</th>
              <th>{t('ies.result.fin_movement_payments')}</th>
              <th>{t('ies.result.fin_end_unpaid')}</th>
            </tr></thead>
            <tbody>{fin.cash_movements.map((item) => (
              <tr key={JSON.stringify([item.id, item.axis, item.period])}>
                <td>{item.id} / {item.axis}={item.period}</td>
                <td>{item.direction} / {item.price_basis}</td>
                <td>{item.raw} {ccy}</td><td>{item.gross} {ccy}</td>
                <td>{item.deductible_input} {ccy}</td><td>{item.net_basis} {ccy}</td>
                <td>{item.output_tax} {ccy}</td><td>{item.net_revenue} {ccy}</td>
                <td>{item.payments.map((payment, index) => (
                  <div key={index}>{timePoint(payment.at)} / {payment.ratio}: {payment.amount} {ccy}</div>
                ))}</td>
                <td>{item.end_unpaid} {ccy}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_assets')}</h3>
      <p>{t('ies.result.fin_assets_note')}</p>
      {fin.assets.length === 0 ? <p>{none}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_assets')}>
            <thead><tr>
              <th>{t('ies.result.fin_asset_id')}</th>
              <th>{t('ies.result.fin_relation_net_basis')}</th>
              <th>{t('ies.result.fin_asset_capitalized')}</th>
              <th>{t('ies.result.fin_asset_available')}</th>
              <th>{t('ies.result.fin_asset_book_values')}</th>
            </tr></thead>
            <tbody>{fin.assets.map((asset) => (
              <tr key={asset.id}>
                <td>{asset.id}</td><td>{asset.basis} {ccy}</td>
                <td>{timePoint(asset.capitalized_at)}</td><td>{asset.available_from}</td>
                <td>{asset.book_values.map((value) => <div key={value.year}>y={value.year}: {value.value} {ccy}</div>)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_tax_vat_rules')}</h3>
      <p>{t('ies.result.fin_tax_vat_rules_note')}</p>
      {fin.tax_cash.vat_rules.length === 0 ? <p>{none}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_vat_rules')}>
            <thead><tr>
              <th>{t('ies.result.fin_tax_rule')}</th><th>{t('ies.result.fin_tax_pool')}</th>
              <th>{t('ies.result.fin_relation_period')}</th><th>{t('ies.result.fin_output_tax')}</th>
              <th>{t('ies.result.fin_relation_deductible')}</th>
            </tr></thead>
            <tbody>{fin.tax_cash.vat_rules.map((row) => (
              <tr key={JSON.stringify([row.rule, row.month])}>
                <td>{row.rule}</td><td>{row.pool}</td><td>m={row.month}</td>
                <td>{row.output_tax} {ccy}</td><td>{row.deductible_input} {ccy}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_tax_derived')}</h3>
      <p>{t('ies.result.fin_tax_derived_note')}</p>
      {fin.tax_cash.derived.length === 0 ? <p>{none}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_derived')}>
            <thead><tr>
              <th>{t('ies.result.fin_tax_rule')}</th><th>{t('ies.result.fin_relation_axis')}</th>
              <th>{t('ies.result.fin_derived_recognition')}</th><th>{t('ies.result.fin_tax_assessed')}</th>
              <th>{t('ies.result.fin_derived_payment')}</th><th>{t('ies.result.fin_tax_paid')}</th>
            </tr></thead>
            <tbody>{fin.tax_cash.derived.map((row) => (
              <tr key={JSON.stringify([row.rule, row.axis, row.period])}>
                <td>{row.rule}</td><td>{row.axis}</td><td>{row.period}</td><td>{row.assessed} {ccy}</td>
                <td>{row.payment_period}</td><td>{row.paid} {ccy}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  </>
}
