/**
 * 结果页二级明细页（完整原始封存数据表，支持筛选、子分页与返回所属一级 Tab）。
 */

import { useEffect, useMemo, useState } from 'react'

import {
  Badge,
  Button,
  Card,
  EmptyState,
  Pagination,
  Select,
  Tabs,
} from '../../components/ui'
import type { TabItem } from '../../components/ui'
import { useI18n } from '../../i18n'
import { formatNumber } from '../../lib/format'
import type { FinancialEvaluationBlock } from '../../types'
import { paginateItems } from './chartMappers'
import type { ResultDetail, ResultTab } from './chartMappers'
import type { HourlyWindowState } from './EngineeringTabView'
import { FinancialComparison } from './FinancialComparison'
import { FinancialRelations } from './FinancialRelations'
import { FinancialTransactions } from './FinancialTransactions'

export interface ResultDetailViewProps {
  detail: ResultDetail
  parentTab: ResultTab
  parentTabLabel: string
  fin: FinancialEvaluationBlock | null
  hourly: HourlyWindowState | null
  hourlyLoading: boolean
  hourlyJump: string
  onHourlyJumpChange: (value: string) => void
  onRetryHourly: (start?: number) => void
  onBack: () => void
}

type VatSubTab = 'pool' | 'rules' | 'refunds'
type IncomeSubTab = 'income' | 'depreciation' | 'losses' | 'derived'

const DETAIL_TITLE_KEY: Record<ResultDetail, string> = {
  cashflows: 'ies.result.detail_cashflows_title',
  operation: 'ies.result.detail_operation_title',
  comparison: 'ies.result.detail_comparison_title',
  demand: 'ies.result.detail_demand_title',
  vat: 'ies.result.detail_vat_title',
  income: 'ies.result.detail_income_title',
  relations: 'ies.result.detail_relations_title',
  transactions: 'ies.result.detail_transactions_title',
  hourly: 'ies.result.hourly_table_title',
}

export function ResultDetailView({
  detail,
  parentTabLabel,
  fin,
  hourly,
  hourlyLoading,
  hourlyJump,
  onHourlyJumpChange,
  onRetryHourly,
  onBack,
}: ResultDetailViewProps) {
  const { t } = useI18n()

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(detail === 'hourly' ? 50 : 20)
  const [afterTaxPage, setAfterTaxPage] = useState(1)
  const [afterTaxPageSize, setAfterTaxPageSize] = useState(20)

  const [demandBinding, setDemandBinding] = useState('all')
  const [demandYear, setDemandYear] = useState('all')
  const [vatSubTab, setVatSubTab] = useState<VatSubTab>('pool')
  const [vatPool, setVatPool] = useState('all')
  const [incomeSubTab, setIncomeSubTab] = useState<IncomeSubTab>('income')
  const [incomeRule, setIncomeRule] = useState('all')

  useEffect(() => {
    setPage(1)
    setAfterTaxPage(1)
    setPageSize(detail === 'hourly' ? 50 : 20)
    setDemandBinding('all')
    setDemandYear('all')
    setVatSubTab('pool')
    setVatPool('all')
    setIncomeSubTab('income')
    setIncomeRule('all')
  }, [detail, fin, hourly?.start])

  useEffect(() => {
    setPage(1)
  }, [demandBinding, demandYear, vatSubTab, vatPool, incomeSubTab, incomeRule])

  const detailTitle = t(DETAIL_TITLE_KEY[detail])
  const ccy = fin?.currency ?? ''

  const demandBindings = useMemo(
    () => Array.from(new Set((fin?.monthly_demand ?? []).map((r) => r.billing_id))),
    [fin?.monthly_demand],
  )
  const demandYears = useMemo(
    () => Array.from(new Set((fin?.monthly_demand ?? []).map((r) => String(r.financial_year)))),
    [fin?.monthly_demand],
  )
  const filteredDemand = useMemo(
    () =>
      (fin?.monthly_demand ?? []).filter((r) => {
        if (demandBinding !== 'all' && r.billing_id !== demandBinding) return false
        if (demandYear !== 'all' && String(r.financial_year) !== demandYear) return false
        return true
      }),
    [fin?.monthly_demand, demandBinding, demandYear],
  )

  const vatPools = useMemo(
    () => Array.from(new Set((fin?.tax_cash.vat ?? []).map((r) => r.pool))),
    [fin?.tax_cash.vat],
  )
  const filteredVat = useMemo(
    () =>
      (fin?.tax_cash.vat ?? []).filter((r) => vatPool === 'all' || r.pool === vatPool),
    [fin?.tax_cash.vat, vatPool],
  )

  const incomeRules = useMemo(
    () => Array.from(new Set((fin?.tax_cash.income ?? []).map((r) => r.rule))),
    [fin?.tax_cash.income],
  )
  const filteredIncome = useMemo(
    () =>
      (fin?.tax_cash.income ?? []).filter((r) => incomeRule === 'all' || r.rule === incomeRule),
    [fin?.tax_cash.income, incomeRule],
  )

  const hourlyIndices = useMemo(() => {
    if (!hourly || hourly.series.length === 0) return []
    const len = hourly.series.reduce(
      (max, meta) => Math.max(max, hourly.flows[meta.series_id]?.length ?? 0),
      0,
    )
    return Array.from({ length: len }, (_, i) => i)
  }, [hourly])

  const renderHeader = (totalLabel?: string) => (
    <div className="ies-result-detail__bar">
      <div className="ies-result-detail__nav">
        <Button variant="secondary" size="sm" onClick={onBack}>
          ← {t('ies.result.back_to_tab', { tab: parentTabLabel })}
        </Button>
        <div>
          <div className="ies-result-detail__crumb">
            {t('ies.nav.results')} / {parentTabLabel}
          </div>
          <h2 className="ies-result-detail__title">{detailTitle}</h2>
        </div>
      </div>
      {totalLabel ? <Badge variant="neutral" label={totalLabel} /> : null}
    </div>
  )

  if (detail === 'hourly') {
    if (!hourly || hourly.series.length === 0 || hourlyIndices.length === 0) {
      return (
        <>
          {renderHeader()}
          <Card title={detailTitle} className="ies-section">
            <EmptyState icon="info" title={t('ies.result.detail_unavailable')} />
          </Card>
        </>
      )
    }
    const slice = paginateItems(hourlyIndices, page, pageSize)
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: hourlyIndices.length }))}
        <Card title={detailTitle} className="ies-section">
          <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.result.hourly_note', {
              n: hourlyIndices.length,
              start: hourly.start,
              end: hourly.end - 1,
              total: hourly.totalRows,
            })}
          </p>
          <div className="ies-form-row" style={{ marginBottom: 'var(--ies-space-3)' }}>
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
            <label>
              {t('ies.result.hourly_start')}
              <input
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
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="ies-metric-table" aria-label={detailTitle}>
              <thead>
                <tr>
                  <th>{t('ies.result.hourly_step_col')}</th>
                  {hourly.series.map((meta) => {
                    const name =
                      meta.kind === 'device'
                        ? `${meta.instance_id}.${meta.variable_id}`
                        : `${t('ies.result.system_series')} ${meta.block}.${meta.variable}`
                    return (
                      <th key={meta.series_id}>
                        {name} ({meta.unit})
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {slice.items.map((localIdx) => (
                  <tr key={localIdx}>
                    <td>{hourly.start + localIdx}</td>
                    {hourly.series.map((meta) => {
                      const val = hourly.flows[meta.series_id]?.[localIdx]
                      return (
                        <td key={meta.series_id}>
                          {val === undefined || val === null ? '—' : formatNumber(val, { digits: 4 })}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={slice.page}
            pageSize={slice.pageSize}
            total={slice.total}
            onPageChange={setPage}
            onPageSizeChange={(next) => {
              setPageSize(next)
              setPage(1)
            }}
            pageSizeOptions={[20, 50, 100, 200]}
          />
        </Card>
      </>
    )
  }

  if (!fin) {
    return (
      <>
        {renderHeader()}
        <Card title={detailTitle} className="ies-section">
          <EmptyState icon="info" title={t('ies.result.detail_unavailable')} />
        </Card>
      </>
    )
  }

  if (detail === 'cashflows') {
    const cfSlice = paginateItems(fin.evaluation.rows, page, pageSize)
    const afterTaxSlice = paginateItems(fin.annual_net_flows, afterTaxPage, afterTaxPageSize)
    const totalCount = Math.max(fin.evaluation.rows.length, fin.annual_net_flows.length)
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: totalCount }))}
        {fin.evaluation.rows.length > 0 ? (
          <Card title={t('ies.result.fin_cf_table')} className="ies-section">
            <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginBottom: 'var(--ies-space-2)' }}>
              {t('ies.result.fin_axis_note')}
            </p>
            <div style={{ overflowX: 'auto' }}>
              <table className="ies-metric-table" aria-label={t('ies.result.fin_cf_table')}>
                <thead>
                  <tr>
                    <th>{t('ies.result.cf_year_col')}</th>
                    <th>{t('ies.result.cf_net')} ({ccy})</th>
                    <th>{t('ies.result.cf_discounted')} ({ccy})</th>
                    <th>{t('ies.result.cf_cumulative')} ({ccy})</th>
                    <th>{t('ies.result.cf_cumulative_discounted')} ({ccy})</th>
                  </tr>
                </thead>
                <tbody>
                  {cfSlice.items.map((r) => (
                    <tr key={r.year}>
                      <td>CF[t={r.year}]</td>
                      <td>{r.net}</td>
                      <td>{r.discounted}</td>
                      <td>{r.cumulative}</td>
                      <td>{r.cumulative_discounted}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={cfSlice.page}
              pageSize={cfSlice.pageSize}
              total={cfSlice.total}
              onPageChange={setPage}
              onPageSizeChange={(next) => {
                setPageSize(next)
                setPage(1)
              }}
              pageSizeOptions={[12, 20, 50, 100]}
            />
          </Card>
        ) : null}
        {fin.annual_net_flows.length > 0 ? (
          <Card title={t('ies.result.fin_tax_flows')} className="ies-section">
            <div style={{ overflowX: 'auto' }}>
              <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_flows')}>
                <thead>
                  <tr>
                    <th>{t('ies.result.cf_year_col')}</th>
                    <th>{t('ies.result.cf_after_tax_col')}</th>
                  </tr>
                </thead>
                <tbody>
                  {afterTaxSlice.items.map((val, localIdx) => {
                    const tIndex = afterTaxSlice.startIndex + localIdx
                    return (
                      <tr key={tIndex}>
                        <td>CF[t={tIndex}]</td>
                        <td>{val} {ccy}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <Pagination
              page={afterTaxSlice.page}
              pageSize={afterTaxSlice.pageSize}
              total={afterTaxSlice.total}
              onPageChange={setAfterTaxPage}
              onPageSizeChange={(next) => {
                setAfterTaxPageSize(next)
                setAfterTaxPage(1)
              }}
              pageSizeOptions={[12, 20, 50, 100]}
            />
          </Card>
        ) : null}
      </>
    )
  }

  if (detail === 'operation') {
    const slice = paginateItems(fin.annual_operation, page, pageSize)
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: fin.annual_operation.length }))}
        <Card title={t('ies.result.fin_yearly')} className="ies-section">
          {fin.annual_operation.length === 0 ? (
            <EmptyState icon="info" title={t('ies.result.fin_tax_none')} />
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table className="ies-metric-table" aria-label={t('ies.result.fin_yearly')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.op_year_col')}</th>
                      <th>{t('ies.result.op_energy_net')} ({ccy})</th>
                      <th>{t('ies.result.op_variable_om')} ({ccy})</th>
                      <th>{t('ies.result.op_fixed_om')} ({ccy})</th>
                      <th>{t('ies.result.op_maintenance')} ({ccy})</th>
                      <th>{t('ies.result.op_demand_charge')} ({ccy})</th>
                      <th>{t('ies.result.op_net')} ({ccy})</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slice.items.map((a, idx) => {
                      const yearNo = slice.startIndex + idx + 1
                      return (
                        <tr key={a.financial_year}>
                          <td>{t('ies.result.fin_year_row', { j: yearNo, year: a.financial_year })}</td>
                          <td>{a.energy_net}</td>
                          <td>{a.variable_om}</td>
                          <td>{a.fixed_om}</td>
                          <td>{a.maintenance}</td>
                          <td>{a.demand_charge}</td>
                          <td>{a.net}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <Pagination
                page={slice.page}
                pageSize={slice.pageSize}
                total={slice.total}
                onPageChange={setPage}
                onPageSizeChange={(next) => {
                  setPageSize(next)
                  setPage(1)
                }}
                pageSizeOptions={[12, 20, 50, 100]}
              />
            </>
          )}
        </Card>
      </>
    )
  }

  if (detail === 'comparison') {
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: fin.annual_net_flows.length }))}
        <Card title={t('ies.result.comparison')} className="ies-section">
          <FinancialComparison fin={fin} paginated defaultPageSize={20} />
        </Card>
      </>
    )
  }

  if (detail === 'demand') {
    const slice = paginateItems(filteredDemand, page, pageSize)
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: filteredDemand.length }))}
        <Card title={t('ies.result.fin_demand_monthly')} className="ies-section">
          {fin.monthly_demand.length === 0 ? (
            <p>{t('ies.result.fin_demand_unbound')}</p>
          ) : (
            <>
              <div className="ies-result-toolbar">
                <div className="ies-result-toolbar__filters">
                  {demandBindings.length > 1 ? (
                    <Select
                      aria-label={t('ies.result.filter_binding')}
                      value={demandBinding}
                      onChange={(e) => setDemandBinding(e.target.value)}
                    >
                      <option value="all">{t('ies.result.filter_binding')}: {t('ies.result.filter_all')}</option>
                      {demandBindings.map((b) => (
                        <option key={b} value={b}>{b}</option>
                      ))}
                    </Select>
                  ) : null}
                  {demandYears.length > 1 ? (
                    <Select
                      aria-label={t('ies.result.filter_year')}
                      value={demandYear}
                      onChange={(e) => setDemandYear(e.target.value)}
                    >
                      <option value="all">{t('ies.result.filter_year')}: {t('ies.result.filter_all')}</option>
                      {demandYears.map((yr) => (
                        <option key={yr} value={yr}>t={yr}</option>
                      ))}
                    </Select>
                  ) : null}
                </div>
              </div>
              <div style={{ overflowX: 'auto' }}>
                <table className="ies-metric-table" aria-label={t('ies.result.fin_demand_monthly')}>
                  <thead>
                    <tr>
                      <th>{t('ies.result.fin_demand_binding')}</th>
                      <th>{t('ies.result.fin_demand_charge')}</th>
                      <th>{t('ies.result.fin_demand_month')}</th>
                      <th>{t('ies.result.fin_demand_year')}</th>
                      <th>{t('ies.result.fin_demand_peak')}</th>
                      <th>{t('ies.result.fin_demand_rate')}</th>
                      <th>{t('ies.result.fin_demand_amount')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slice.items.map((r) => (
                      <tr key={`${r.billing_id}-${r.calendar_year}-${r.calendar_month}`}>
                        <td>{r.billing_id}</td>
                        <td>{r.charge_id}</td>
                        <td>{r.calendar_year}-{String(r.calendar_month).padStart(2, '0')}</td>
                        <td>{r.financial_year}</td>
                        <td>{r.peak_power} {r.peak_unit}</td>
                        <td>{r.rate.value} {r.rate.unit}</td>
                        <td>{r.charge} {ccy}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination
                page={slice.page}
                pageSize={slice.pageSize}
                total={slice.total}
                onPageChange={setPage}
                onPageSizeChange={(next) => {
                  setPageSize(next)
                  setPage(1)
                }}
                pageSizeOptions={[12, 20, 24, 50, 100]}
              />
              <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginTop: 'var(--ies-space-2)' }}>
                {t('ies.result.fin_demand_zero_note')}
              </p>
            </>
          )}
        </Card>
      </>
    )
  }

  if (detail === 'vat') {
    const vatSubTabs: TabItem<VatSubTab>[] = [
      { id: 'pool', label: t('ies.result.tax_vat_pool_tab'), badge: fin.tax_cash.vat.length },
      { id: 'rules', label: t('ies.result.tax_vat_rules_tab'), badge: fin.tax_cash.vat_rules.length },
      { id: 'refunds', label: t('ies.result.tax_vat_refunds_tab'), badge: fin.tax_cash.refunds.length },
    ]
    const vatSlice = paginateItems(filteredVat, page, pageSize)
    const refundSlice = paginateItems(fin.tax_cash.refunds, page, pageSize)
    const horizonMonths = fin.evaluation_years * 12

    return (
      <>
        {renderHeader(
          t('ies.result.count_items', {
            count: fin.tax_cash.vat.length + fin.tax_cash.vat_rules.length + fin.tax_cash.refunds.length,
          }),
        )}
        <Card title={t('ies.result.detail_vat_title')} className="ies-section">
          <Tabs
            items={vatSubTabs}
            activeId={vatSubTab}
            onChange={setVatSubTab}
            ariaLabel={t('ies.result.detail_vat_title')}
            idPrefix="vat-detail"
          />
          {vatSubTab === 'pool' ? (
            <>
              {vatPools.length > 1 ? (
                <div className="ies-result-toolbar">
                  <div className="ies-result-toolbar__filters">
                    <Select
                      aria-label={t('ies.result.filter_pool')}
                      value={vatPool}
                      onChange={(e) => setVatPool(e.target.value)}
                    >
                      <option value="all">{t('ies.result.filter_pool')}: {t('ies.result.filter_all')}</option>
                      {vatPools.map((p) => (
                        <option key={p} value={p}>{p}</option>
                      ))}
                    </Select>
                  </div>
                </div>
              ) : null}
              {filteredVat.length > 0 ? (
                <>
                  <div style={{ overflowX: 'auto' }}>
                    <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_vat')}>
                      <thead>
                        <tr>
                          <th>{t('ies.result.fin_tax_pool')}</th>
                          <th>{t('ies.result.fin_relation_period')}</th>
                          <th>{t('ies.result.fin_tax_paid')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_claimed')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_arrived')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_carry')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_receivable')} ({ccy})</th>
                        </tr>
                      </thead>
                      <tbody>
                        {vatSlice.items.map((row, index) => (
                          <tr key={`${row.pool}-${row.month}-${index}`}>
                            <td>{row.pool}</td>
                            <td>m={row.month}</td>
                            <td>{row.paid}</td>
                            <td>{row.refund_claimed}</td>
                            <td>{row.refund_arrived}</td>
                            <td>{row.carry}</td>
                            <td>{row.receivable}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <Pagination
                    page={vatSlice.page}
                    pageSize={vatSlice.pageSize}
                    total={vatSlice.total}
                    onPageChange={setPage}
                    onPageSizeChange={(next) => {
                      setPageSize(next)
                      setPage(1)
                    }}
                    pageSizeOptions={[12, 20, 24, 50, 100]}
                  />
                </>
              ) : (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.result.fin_tax_none')}
                </p>
              )}
            </>
          ) : null}

          {vatSubTab === 'rules' ? (
            <FinancialTransactions fin={fin} sections={['vat_rules']} paginated defaultPageSize={20} />
          ) : null}

          {vatSubTab === 'refunds' ? (
            fin.tax_cash.refunds.length > 0 ? (
              <>
                <div style={{ overflowX: 'auto' }}>
                  <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_refunds')}>
                    <tbody>
                      {refundSlice.items.map((row, index) => (
                        <tr key={`${row.pool}-${row.period}-${index}`}>
                          <td>
                            {row.pool} / {t('ies.result.fin_tax_period', { p: row.period })} →{' '}
                            {t('ies.result.fin_tax_arrival', { p: row.arrival_period })}
                            {row.arrival_period > horizonMonths ? t('ies.result.fin_tax_post_receivable') : ''}
                          </td>
                          <td>{row.amount} {ccy}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  page={refundSlice.page}
                  pageSize={refundSlice.pageSize}
                  total={refundSlice.total}
                  onPageChange={setPage}
                  onPageSizeChange={(next) => {
                    setPageSize(next)
                    setPage(1)
                  }}
                  pageSizeOptions={[12, 20, 50, 100]}
                />
              </>
            ) : (
              <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                {t('ies.result.fin_tax_none')}
              </p>
            )
          ) : null}
        </Card>
      </>
    )
  }

  if (detail === 'income') {
    const incomeSubTabs: TabItem<IncomeSubTab>[] = [
      { id: 'income', label: t('ies.result.tax_income_tab'), badge: fin.tax_cash.income.length },
      { id: 'depreciation', label: t('ies.result.tax_depreciation_tab'), badge: fin.tax_cash.depreciation.length },
      { id: 'losses', label: t('ies.result.tax_losses_tab'), badge: fin.tax_cash.loss_balances.length },
      { id: 'derived', label: t('ies.result.tax_derived_tab'), badge: fin.tax_cash.derived.length },
    ]
    const incomeSlice = paginateItems(filteredIncome, page, pageSize)
    const depSlice = paginateItems(fin.tax_cash.depreciation, page, pageSize)
    const lossSlice = paginateItems(fin.tax_cash.loss_balances, page, pageSize)

    return (
      <>
        {renderHeader(
          t('ies.result.count_items', {
            count:
              fin.tax_cash.income.length +
              fin.tax_cash.depreciation.length +
              fin.tax_cash.loss_balances.length +
              fin.tax_cash.derived.length,
          }),
        )}
        <Card title={t('ies.result.detail_income_title')} className="ies-section">
          <Tabs
            items={incomeSubTabs}
            activeId={incomeSubTab}
            onChange={setIncomeSubTab}
            ariaLabel={t('ies.result.detail_income_title')}
            idPrefix="income-detail"
          />
          {incomeSubTab === 'income' ? (
            <>
              {incomeRules.length > 1 ? (
                <div className="ies-result-toolbar">
                  <div className="ies-result-toolbar__filters">
                    <Select
                      aria-label={t('ies.result.filter_rule')}
                      value={incomeRule}
                      onChange={(e) => setIncomeRule(e.target.value)}
                    >
                      <option value="all">{t('ies.result.filter_rule')}: {t('ies.result.filter_all')}</option>
                      {incomeRules.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </Select>
                  </div>
                </div>
              ) : null}
              {filteredIncome.length > 0 ? (
                <>
                  <div style={{ overflowX: 'auto' }}>
                    <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_income')}>
                      <thead>
                        <tr>
                          <th>{t('ies.result.fin_tax_rule')}</th>
                          <th>{t('ies.result.fin_demand_year')}</th>
                          <th>{t('ies.result.fin_tax_base')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_used')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_generated')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_expired')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_assessed')} ({ccy})</th>
                          <th>{t('ies.result.fin_tax_tax')} ({ccy})</th>
                        </tr>
                      </thead>
                      <tbody>
                        {incomeSlice.items.map((row, index) => (
                          <tr key={`${row.rule}-${row.year}-${index}`}>
                            <td>{row.rule}</td>
                            <td>y={row.year}</td>
                            <td>{row.taxable_base}</td>
                            <td>{row.loss_used}</td>
                            <td>{row.loss_generated}</td>
                            <td>{row.loss_expired}</td>
                            <td>{row.assessed}</td>
                            <td>{row.tax}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <Pagination
                    page={incomeSlice.page}
                    pageSize={incomeSlice.pageSize}
                    total={incomeSlice.total}
                    onPageChange={setPage}
                    onPageSizeChange={(next) => {
                      setPageSize(next)
                      setPage(1)
                    }}
                    pageSizeOptions={[12, 20, 50, 100]}
                  />
                </>
              ) : (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.result.fin_tax_none')}
                </p>
              )}
            </>
          ) : null}

          {incomeSubTab === 'depreciation' ? (
            <>
              {fin.tax_cash.depreciation.length > 0 ? (
                <>
                  <div style={{ overflowX: 'auto' }}>
                    <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_depreciation')}>
                      <tbody>
                        {depSlice.items.map((row, index) => (
                          <tr key={`${row.rule}-${row.year}-${index}`}>
                            <td>
                              {row.rule} /{' '}
                              {row.base_from.kind === 'upfront_capex_net'
                                ? `${row.base_from.kind}: ${row.base_from.finance_type}`
                                : row.base_from.kind === 'asset'
                                  ? `asset: ${row.base_from.id}`
                                  : `${row.base_from.kind}: y=${row.base_from.year}, #${row.base_from.position}`}{' '}
                              / y={row.year}
                            </td>
                            <td>{row.amount} {ccy}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <Pagination
                    page={depSlice.page}
                    pageSize={depSlice.pageSize}
                    total={depSlice.total}
                    onPageChange={setPage}
                    onPageSizeChange={(next) => {
                      setPageSize(next)
                      setPage(1)
                    }}
                    pageSizeOptions={[12, 20, 50, 100]}
                  />
                </>
              ) : (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.result.fin_tax_none')}
                </p>
              )}
              <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginTop: 'var(--ies-space-2)' }}>
                {t('ies.result.fin_tax_depreciation_note')}
              </p>
            </>
          ) : null}

          {incomeSubTab === 'losses' ? (
            fin.tax_cash.loss_balances.length > 0 ? (
              <>
                <div style={{ overflowX: 'auto' }}>
                  <table className="ies-metric-table" aria-label={t('ies.result.fin_tax_balances')}>
                    <tbody>
                      {lossSlice.items.map((row, index) => (
                        <tr key={`${row.rule}-${row.vintage_id}-${row.year}-${index}`}>
                          <td>
                            {row.rule} / {row.vintage_id} / y={row.year} /{' '}
                            {t('ies.result.fin_tax_last_usable', { y: row.last_usable_year })}
                          </td>
                          <td>{row.amount} {ccy}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  page={lossSlice.page}
                  pageSize={lossSlice.pageSize}
                  total={lossSlice.total}
                  onPageChange={setPage}
                  onPageSizeChange={(next) => {
                    setPageSize(next)
                    setPage(1)
                  }}
                  pageSizeOptions={[12, 20, 50, 100]}
                />
              </>
            ) : (
              <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                {t('ies.result.fin_tax_none')}
              </p>
            )
          ) : null}

          {incomeSubTab === 'derived' ? (
            <FinancialTransactions fin={fin} sections={['derived']} paginated defaultPageSize={20} />
          ) : null}
        </Card>
      </>
    )
  }

  if (detail === 'relations') {
    const amountCount = fin.amounts.reduce((acc, a) => acc + a.values.length, 0)
    return (
      <>
        {renderHeader(t('ies.result.count_items', { count: fin.cost_items.length + amountCount }))}
        <Card title={t('ies.result.detail_relations_title')} className="ies-section">
          <FinancialRelations costItems={fin.cost_items} amounts={fin.amounts} paginated defaultPageSize={20} />
        </Card>
      </>
    )
  }

  return (
    <>
      {renderHeader(t('ies.result.count_items', { count: fin.cash_movements.length + fin.assets.length }))}
      <Card title={t('ies.result.detail_transactions_title')} className="ies-section">
        <FinancialTransactions fin={fin} sections={['movements', 'assets']} paginated defaultPageSize={20} />
      </Card>
    </>
  )
}
