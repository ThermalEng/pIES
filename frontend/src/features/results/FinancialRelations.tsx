import { useI18n } from '../../i18n'
import type { FinancialCostItem, FinancialNamedAmount } from '../../types'

/** Frozen relation facts are explanations, not additional cashflow entries. */
export function FinancialRelations({ costItems, amounts }: {
  costItems: FinancialCostItem[]
  amounts: FinancialNamedAmount[]
}) {
  const { t } = useI18n()
  return <>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_cost_items')}</h3>
      <p>{t('ies.result.fin_relations_note')}</p>
      {costItems.length === 0 ? <p>{t('ies.result.fin_cost_items_none')}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_cost_items')}>
            <thead><tr>
              <th>{t('ies.result.fin_construction_instance')}</th>
              <th>{t('ies.result.fin_relation_cost_identity')}</th>
              <th>{t('ies.result.fin_relation_axis')}</th>
              <th>{t('ies.result.fin_relation_period')}</th>
              <th>{t('ies.result.fin_relation_raw')}</th>
              <th>{t('ies.result.fin_relation_gross')}</th>
              <th>{t('ies.result.fin_relation_deductible')}</th>
              <th>{t('ies.result.fin_relation_net_basis')}</th>
              <th>{t('ies.result.fin_relation_unit')}</th>
            </tr></thead>
            <tbody>{costItems.map((item) => (
              <tr key={JSON.stringify([item.instance_id, item.component, item.item_id, item.axis, item.period])}>
                <td>{item.instance_id}</td>
                <td>{item.finance_type} / {item.component} / {item.item_id}</td>
                <td>{item.axis}</td><td>{item.period}</td>
                <td>{item.raw}</td><td>{item.gross}</td>
                <td>{item.deductible_input}</td><td>{item.net_basis}</td><td>{item.unit}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
    <section style={{ marginTop: 'var(--ies-space-3)' }}>
      <h3>{t('ies.result.fin_amounts')}</h3>
      {amounts.length === 0 ? <p>{t('ies.result.fin_amounts_none')}</p> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="ies-metric-table" aria-label={t('ies.result.fin_amounts')}>
            <thead><tr>
              <th>{t('ies.result.fin_relation_amount_id')}</th>
              <th>{t('ies.result.fin_relation_axis')}</th>
              <th>{t('ies.result.fin_relation_period')}</th>
              <th>{t('ies.result.fin_relation_value')}</th>
              <th>{t('ies.result.fin_relation_unit')}</th>
            </tr></thead>
            <tbody>{amounts.flatMap((amount) => amount.values.map((value) => (
              <tr key={JSON.stringify([amount.id, value.period])}>
                <td>{amount.id}</td><td>{amount.axis}</td><td>{value.period}</td>
                <td>{value.value}</td><td>{amount.unit}</td>
              </tr>
            )))}</tbody>
          </table>
        </div>
      )}
    </section>
  </>
}
