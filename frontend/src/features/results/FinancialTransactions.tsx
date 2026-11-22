import { useEffect, useMemo, useState } from 'react'

import { Input, Pagination, Select } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { FinancialEvaluationBlock, FinancialTimePoint } from '../../types'
import { paginateItems } from './chartMappers'

function timePoint(point: FinancialTimePoint): string {
  return point.kind === 'financial_year' ? `financial_year=${point.year}` : `tax_month=${point.month}`
}

export type TransactionSectionKey = 'movements' | 'assets' | 'vat_rules' | 'derived'

export interface FinancialTransactionsProps {
  fin: FinancialEvaluationBlock
  /** 启用表格筛选与分页；默认 false 以保持静态渲染单测兼容。 */
  paginated?: boolean
  defaultPageSize?: number
  /** 指定要展示的子节；缺省展示全部四个子节。 */
  sections?: readonly TransactionSectionKey[]
}

/** Occurrence, payment, asset and tax facts stay distinct; no totals are rebuilt. */
export function FinancialTransactions({
  fin,
  paginated = false,
  defaultPageSize = 20,
  sections,
}: FinancialTransactionsProps) {
  const { t } = useI18n()
  const ccy = fin.currency
  const none = t('ies.result.fin_tax_none')
  const enabled = useMemo(
    () => new Set<TransactionSectionKey>(sections ?? ['movements', 'assets', 'vat_rules', 'derived']),
    [sections],
  )

  const [movQuery, setMovQuery] = useState('')
  const [movPage, setMovPage] = useState(1)
  const [movPageSize, setMovPageSize] = useState(defaultPageSize)

  const [assetPage, setAssetPage] = useState(1)
  const [assetPageSize, setAssetPageSize] = useState(defaultPageSize)

  const [vatRuleFilter, setVatRuleFilter] = useState('all')
  const [vatRulePage, setVatRulePage] = useState(1)
  const [vatRulePageSize, setVatRulePageSize] = useState(defaultPageSize)

  const [derivedRuleFilter, setDerivedRuleFilter] = useState('all')
  const [derivedPage, setDerivedPage] = useState(1)
  const [derivedPageSize, setDerivedPageSize] = useState(defaultPageSize)

  useEffect(() => {
    setMovPage(1)
  }, [movQuery, fin.cash_movements])
  useEffect(() => {
    setAssetPage(1)
  }, [fin.assets])
  useEffect(() => {
    setVatRulePage(1)
  }, [vatRuleFilter, fin.tax_cash.vat_rules])
  useEffect(() => {
    setDerivedPage(1)
  }, [derivedRuleFilter, fin.tax_cash.derived])

  const filteredMovements = useMemo(() => {
    if (!paginated) return fin.cash_movements
    const q = movQuery.trim().toLowerCase()
    if (!q) return fin.cash_movements
    return fin.cash_movements.filter(
      (it) => it.id.toLowerCase().includes(q) || it.axis.toLowerCase().includes(q),
    )
  }, [paginated, fin.cash_movements, movQuery])

  const vatRuleNames = useMemo(
    () => Array.from(new Set(fin.tax_cash.vat_rules.map((r) => r.rule))),
    [fin.tax_cash.vat_rules],
  )
  const filteredVatRules = useMemo(() => {
    if (!paginated || vatRuleFilter === 'all') return fin.tax_cash.vat_rules
    return fin.tax_cash.vat_rules.filter((r) => r.rule === vatRuleFilter)
  }, [paginated, fin.tax_cash.vat_rules, vatRuleFilter])

  const derivedRuleNames = useMemo(
    () => Array.from(new Set(fin.tax_cash.derived.map((r) => r.rule))),
    [fin.tax_cash.derived],
  )
  const filteredDerived = useMemo(() => {
    if (!paginated || derivedRuleFilter === 'all') return fin.tax_cash.derived
    return fin.tax_cash.derived.filter((r) => r.rule === derivedRuleFilter)
  }, [paginated, fin.tax_cash.derived, derivedRuleFilter])

  const movSlice = paginated
    ? paginateItems(filteredMovements, movPage, movPageSize)
    : { items: filteredMovements, page: 1, pageSize: filteredMovements.length || 1, total: filteredMovements.length, totalPages: 1 }
  const assetSlice = paginated
    ? paginateItems(fin.assets, assetPage, assetPageSize)
    : { items: fin.assets, page: 1, pageSize: fin.assets.length || 1, total: fin.assets.length, totalPages: 1 }
  const vatRuleSlice = paginated
    ? paginateItems(filteredVatRules, vatRulePage, vatRulePageSize)
    : { items: filteredVatRules, page: 1, pageSize: filteredVatRules.length || 1, total: filteredVatRules.length, totalPages: 1 }
  const derivedSlice = paginated
    ? paginateItems(filteredDerived, derivedPage, derivedPageSize)
    : { items: filteredDerived, page: 1, pageSize: filteredDerived.length || 1, total: filteredDerived.length, totalPages: 1 }

  return (
    <>
      {enabled.has('movements') ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 className="ies-section-subtitle">{t('ies.result.fin_movements')}</h3>
          <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>{t('ies.result.fin_movements_note')}</p>
          {paginated && fin.cash_movements.length > 0 ? (
            <div className="ies-result-toolbar" style={{ marginTop: 'var(--ies-space-2)' }}>
              <div className="ies-result-toolbar__filters">
                <Input
                  type="search"
                  aria-label={t('ies.result.filter_search')}
                  placeholder={t('ies.result.filter_search')}
                  value={movQuery}
                  onChange={(e) => setMovQuery(e.target.value)}
                />
              </div>
            </div>
          ) : null}
          {fin.cash_movements.length === 0 ? (
            <p className="ies-text-secondary">{none}</p>
          ) : (
            <>
              <div className="ies-table-wrap">
                <table className="ies-metric-table" aria-label={t('ies.result.fin_movements')}>
                  <thead>
                    <tr>
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
                    </tr>
                  </thead>
                  <tbody>
                    {movSlice.items.map((item) => (
                      <tr key={JSON.stringify([item.id, item.axis, item.period])}>
                        <td>{item.id} / {item.axis}={item.period}</td>
                        <td>{item.direction} / {item.price_basis}</td>
                        <td>{item.raw} {ccy}</td>
                        <td>{item.gross} {ccy}</td>
                        <td>{item.deductible_input} {ccy}</td>
                        <td>{item.net_basis} {ccy}</td>
                        <td>{item.output_tax} {ccy}</td>
                        <td>{item.net_revenue} {ccy}</td>
                        <td>
                          {item.payments.map((payment, index) => (
                            <div key={index}>{timePoint(payment.at)} / {payment.ratio}: {payment.amount} {ccy}</div>
                          ))}
                        </td>
                        <td>{item.end_unpaid} {ccy}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={movSlice.page}
                  pageSize={movSlice.pageSize}
                  total={movSlice.total}
                  onPageChange={setMovPage}
                  onPageSizeChange={(next) => {
                    setMovPageSize(next)
                    setMovPage(1)
                  }}
                />
              ) : null}
            </>
          )}
        </section>
      ) : null}
      {enabled.has('assets') ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 className="ies-section-subtitle">{t('ies.result.fin_assets')}</h3>
          <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>{t('ies.result.fin_assets_note')}</p>
          {fin.assets.length === 0 ? (
            <p className="ies-text-secondary">{none}</p>
          ) : (
            <>
              <div className="ies-table-wrap">
                <table className="ies-metric-table" aria-label={t('ies.result.fin_assets')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_asset_id')}</th>
                      <th>{t('ies.result.fin_relation_net_basis')}</th>
                      <th>{t('ies.result.fin_asset_capitalized')}</th>
                      <th>{t('ies.result.fin_asset_available')}</th>
                      <th>{t('ies.result.fin_asset_book_values')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {assetSlice.items.map((asset) => (
                      <tr key={asset.id}>
                        <td>{asset.id}</td>
                        <td>{asset.basis} {ccy}</td>
                        <td>{timePoint(asset.capitalized_at)}</td>
                        <td>{asset.available_from}</td>
                        <td>{asset.book_values.map((value) => <div key={value.year}>y={value.year}: {value.value} {ccy}</div>)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={assetSlice.page}
                  pageSize={assetSlice.pageSize}
                  total={assetSlice.total}
                  onPageChange={setAssetPage}
                  onPageSizeChange={(next) => {
                    setAssetPageSize(next)
                    setAssetPage(1)
                  }}
                />
              ) : null}
            </>
          )}
        </section>
      ) : null}
      {enabled.has('vat_rules') ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 className="ies-section-subtitle">{t('ies.result.fin_tax_vat_rules')}</h3>
          <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>{t('ies.result.fin_tax_vat_rules_note')}</p>
          {paginated && vatRuleNames.length > 1 ? (
            <div className="ies-result-toolbar" style={{ marginTop: 'var(--ies-space-2)' }}>
              <div className="ies-result-toolbar__filters">
                <Select
                  aria-label={t('ies.result.filter_rule')}
                  value={vatRuleFilter}
                  onChange={(e) => setVatRuleFilter(e.target.value)}
                >
                  <option value="all">{t('ies.result.filter_rule')}: {t('ies.result.filter_all')}</option>
                  {vatRuleNames.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </Select>
              </div>
            </div>
          ) : null}
          {fin.tax_cash.vat_rules.length === 0 ? (
            <p className="ies-text-secondary">{none}</p>
          ) : (
            <>
              <div className="ies-table-wrap">
                <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_vat_rules')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_tax_rule')}</th>
                      <th>{t('ies.result.fin_tax_pool')}</th>
                      <th>{t('ies.result.fin_relation_period')}</th>
                      <th>{t('ies.result.fin_output_tax')}</th>
                      <th>{t('ies.result.fin_relation_deductible')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vatRuleSlice.items.map((row) => (
                      <tr key={JSON.stringify([row.rule, row.month])}>
                        <td>{row.rule}</td>
                        <td>{row.pool}</td>
                        <td>m={row.month}</td>
                        <td>{row.output_tax} {ccy}</td>
                        <td>{row.deductible_input} {ccy}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={vatRuleSlice.page}
                  pageSize={vatRuleSlice.pageSize}
                  total={vatRuleSlice.total}
                  onPageChange={setVatRulePage}
                  onPageSizeChange={(next) => {
                    setVatRulePageSize(next)
                    setVatRulePage(1)
                  }}
                />
              ) : null}
            </>
          )}
        </section>
      ) : null}
      {enabled.has('derived') ? (
        <section style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 className="ies-section-subtitle">{t('ies.result.fin_tax_derived')}</h3>
          <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-2)' }}>{t('ies.result.fin_tax_derived_note')}</p>
          {paginated && derivedRuleNames.length > 1 ? (
            <div className="ies-result-toolbar" style={{ marginTop: 'var(--ies-space-2)' }}>
              <div className="ies-result-toolbar__filters">
                <Select
                  aria-label={t('ies.result.filter_rule')}
                  value={derivedRuleFilter}
                  onChange={(e) => setDerivedRuleFilter(e.target.value)}
                >
                  <option value="all">{t('ies.result.filter_rule')}: {t('ies.result.filter_all')}</option>
                  {derivedRuleNames.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </Select>
              </div>
            </div>
          ) : null}
          {fin.tax_cash.derived.length === 0 ? (
            <p className="ies-text-secondary">{none}</p>
          ) : (
            <>
              <div className="ies-table-wrap">
                <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_derived')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_tax_rule')}</th>
                      <th>{t('ies.result.fin_relation_axis')}</th>
                      <th>{t('ies.result.fin_derived_recognition')}</th>
                      <th>{t('ies.result.fin_tax_assessed')}</th>
                      <th>{t('ies.result.fin_derived_payment')}</th>
                      <th>{t('ies.result.fin_tax_paid')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {derivedSlice.items.map((row) => (
                      <tr key={JSON.stringify([row.rule, row.axis, row.period])}>
                        <td>{row.rule}</td>
                        <td>{row.axis}</td>
                        <td>{row.period}</td>
                        <td>{row.assessed} {ccy}</td>
                        <td>{row.payment_period}</td>
                        <td>{row.paid} {ccy}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {paginated ? (
                <Pagination
                  page={derivedSlice.page}
                  pageSize={derivedSlice.pageSize}
                  total={derivedSlice.total}
                  onPageChange={setDerivedPage}
                  onPageSizeChange={(next) => {
                    setDerivedPageSize(next)
                    setDerivedPage(1)
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
