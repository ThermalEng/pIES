import { useEffect, useMemo, useState } from 'react'

import { Input, Pagination, Select } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { FinancialCostItem, FinancialNamedAmount } from '../../types'
import { paginateItems } from './chartMappers'

export interface FinancialRelationsProps {
  costItems: FinancialCostItem[]
  amounts: FinancialNamedAmount[]
  /** 启用表格筛选与分页；默认 false 以保持静态快照与轻量单测兼容。 */
  paginated?: boolean
  defaultPageSize?: number
  section?: 'all' | 'cost_items' | 'amounts'
}

/** Frozen relation facts are explanations, not additional cashflow entries. */
export function FinancialRelations({
  costItems,
  amounts,
  paginated = false,
  defaultPageSize = 20,
  section = 'all',
}: FinancialRelationsProps) {
  const { t } = useI18n()

  const [costAxis, setCostAxis] = useState<string>('all')
  const [costQuery, setCostQuery] = useState('')
  const [costPage, setCostPage] = useState(1)
  const [costPageSize, setCostPageSize] = useState(defaultPageSize)

  const [amtAxis, setAmtAxis] = useState<string>('all')
  const [amtQuery, setAmtQuery] = useState('')
  const [amtPage, setAmtPage] = useState(1)
  const [amtPageSize, setAmtPageSize] = useState(defaultPageSize)

  useEffect(() => {
    setCostPage(1)
  }, [costAxis, costQuery, costItems])

  useEffect(() => {
    setAmtPage(1)
  }, [amtAxis, amtQuery, amounts])

  const filteredCostItems = useMemo(() => {
    if (!paginated) return costItems
    const q = costQuery.trim().toLowerCase()
    return costItems.filter((item) => {
      if (costAxis !== 'all' && item.axis !== costAxis) return false
      if (!q) return true
      return (
        item.instance_id.toLowerCase().includes(q) ||
        item.finance_type.toLowerCase().includes(q) ||
        item.component.toLowerCase().includes(q) ||
        item.item_id.toLowerCase().includes(q)
      )
    })
  }, [paginated, costItems, costAxis, costQuery])

  const flatAmounts = useMemo(
    () =>
      amounts.flatMap((amount) =>
        amount.values.map((value) => ({
          id: amount.id,
          axis: amount.axis,
          unit: amount.unit,
          period: value.period,
          value: value.value,
        })),
      ),
    [amounts],
  )

  const filteredAmounts = useMemo(() => {
    if (!paginated) return flatAmounts
    const q = amtQuery.trim().toLowerCase()
    return flatAmounts.filter((row) => {
      if (amtAxis !== 'all' && row.axis !== amtAxis) return false
      if (!q) return true
      return row.id.toLowerCase().includes(q) || row.unit.toLowerCase().includes(q)
    })
  }, [paginated, flatAmounts, amtAxis, amtQuery])

  const costSlice = paginated
    ? paginateItems(filteredCostItems, costPage, costPageSize)
    : { items: filteredCostItems, page: 1, pageSize: filteredCostItems.length || 1, total: filteredCostItems.length, totalPages: 1, startIndex: 0, endIndex: filteredCostItems.length }

  const amtSlice = paginated
    ? paginateItems(filteredAmounts, amtPage, amtPageSize)
    : { items: filteredAmounts, page: 1, pageSize: filteredAmounts.length || 1, total: filteredAmounts.length, totalPages: 1, startIndex: 0, endIndex: filteredAmounts.length }

  const showCostItems = section === 'all' || section === 'cost_items'
  const showAmounts = section === 'all' || section === 'amounts'

  return (
    <>
      {showCostItems ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3>{t('ies.result.fin_cost_items')}</h3>
          <p>{t('ies.result.fin_relations_note')}</p>
          {paginated && costItems.length > 0 ? (
            <div className="ies-result-toolbar" style={{ marginTop: 'var(--ies-space-2)' }}>
              <div className="ies-result-toolbar__filters">
                <Select
                  aria-label={t('ies.result.filter_axis')}
                  value={costAxis}
                  onChange={(e) => setCostAxis(e.target.value)}
                >
                  <option value="all">{t('ies.result.filter_axis')}: {t('ies.result.filter_all')}</option>
                  <option value="scalar">scalar</option>
                  <option value="financial_year">financial_year</option>
                  <option value="tax_month">tax_month</option>
                </Select>
                <Input
                  type="search"
                  aria-label={t('ies.result.filter_search')}
                  placeholder={t('ies.result.filter_search')}
                  value={costQuery}
                  onChange={(e) => setCostQuery(e.target.value)}
                />
              </div>
            </div>
          ) : null}
          {costItems.length === 0 ? (
            <p>{t('ies.result.fin_cost_items_none')}</p>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table className="ies-metric-table" aria-label={t('ies.result.fin_cost_items')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_construction_instance')}</th>
                      <th>{t('ies.result.fin_relation_cost_identity')}</th>
                      <th>{t('ies.result.fin_relation_axis')}</th>
                      <th>{t('ies.result.fin_relation_period')}</th>
                      <th>{t('ies.result.fin_relation_raw')}</th>
                      <th>{t('ies.result.fin_relation_gross')}</th>
                      <th>{t('ies.result.fin_relation_deductible')}</th>
                      <th>{t('ies.result.fin_relation_net_basis')}</th>
                      <th>{t('ies.result.fin_relation_unit')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {costSlice.items.map((item) => (
                      <tr key={JSON.stringify([item.instance_id, item.component, item.item_id, item.axis, item.period])}>
                        <td>{item.instance_id}</td>
                        <td>{item.finance_type} / {item.component} / {item.item_id}</td>
                        <td>{item.axis}</td>
                        <td>{item.period}</td>
                        <td>{item.raw}</td>
                        <td>{item.gross}</td>
                        <td>{item.deductible_input}</td>
                        <td>{item.net_basis}</td>
                        <td>{item.unit}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={costSlice.page}
                  pageSize={costSlice.pageSize}
                  total={costSlice.total}
                  onPageChange={setCostPage}
                  onPageSizeChange={(next) => {
                    setCostPageSize(next)
                    setCostPage(1)
                  }}
                />
              ) : null}
            </>
          )}
        </section>
      ) : null}
      {showAmounts ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3>{t('ies.result.fin_amounts')}</h3>
          {paginated && amounts.length > 0 ? (
            <div className="ies-result-toolbar" style={{ marginTop: 'var(--ies-space-2)' }}>
              <div className="ies-result-toolbar__filters">
                <Select
                  aria-label={t('ies.result.filter_axis')}
                  value={amtAxis}
                  onChange={(e) => setAmtAxis(e.target.value)}
                >
                  <option value="all">{t('ies.result.filter_axis')}: {t('ies.result.filter_all')}</option>
                  <option value="scalar">scalar</option>
                  <option value="financial_year">financial_year</option>
                  <option value="tax_month">tax_month</option>
                </Select>
                <Input
                  type="search"
                  aria-label={t('ies.result.filter_search')}
                  placeholder={t('ies.result.filter_search')}
                  value={amtQuery}
                  onChange={(e) => setAmtQuery(e.target.value)}
                />
              </div>
            </div>
          ) : null}
          {amounts.length === 0 ? (
            <p>{t('ies.result.fin_amounts_none')}</p>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table className="ies-metric-table" aria-label={t('ies.result.fin_amounts')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_relation_amount_id')}</th>
                      <th>{t('ies.result.fin_relation_axis')}</th>
                      <th>{t('ies.result.fin_relation_period')}</th>
                      <th>{t('ies.result.fin_relation_value')}</th>
                      <th>{t('ies.result.fin_relation_unit')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {amtSlice.items.map((row) => (
                      <tr key={JSON.stringify([row.id, row.axis, row.period])}>
                        <td>{row.id}</td>
                        <td>{row.axis}</td>
                        <td>{row.period}</td>
                        <td>{row.value}</td>
                        <td>{row.unit}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={amtSlice.page}
                  pageSize={amtSlice.pageSize}
                  total={amtSlice.total}
                  onPageChange={setAmtPage}
                  onPageSizeChange={(next) => {
                    setAmtPageSize(next)
                    setAmtPage(1)
                  }}
                />
              ) : null}
            </>
          )}
        </section>
      ) : null}
    </>
  )
}
