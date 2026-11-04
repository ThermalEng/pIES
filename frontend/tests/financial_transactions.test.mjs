import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)
const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const { I18nProvider } = await import('../src/i18n/index.ts')
const { FinancialTransactions } = await import('../src/features/results/FinancialTransactions.tsx')

const facts = {
  currency: 'CNY',
  cash_movements: [
    { id: 'purchase', axis: 'financial_year', period: 0, direction: 'outflow', price_basis: 'tax_exclusive',
      raw: '100.000000000000000000001', gross: '110', deductible_input: '10', net_basis: '100', output_tax: '0', net_revenue: '110',
      payments: [{ at: { kind: 'financial_year', year: 0 }, ratio: '0.4', amount: '44' }, { at: { kind: 'financial_year', year: 4 }, ratio: '0.6', amount: '66' }], end_unpaid: '66' },
    { id: 'service', axis: 'tax_month', period: 13, direction: 'outflow', price_basis: 'tax_inclusive',
      raw: '-1.250000000000000001', gross: '-1.250000000000000001', deductible_input: '0', net_basis: '-1.250000000000000001', output_tax: '0', net_revenue: '-1.250000000000000001',
      payments: [{ at: { kind: 'tax_month', month: 14 }, ratio: '1', amount: '-1.250000000000000001' }], end_unpaid: '0' },
  ],
  assets: [{ id: 'asset_a', basis: '100', capitalized_at: { kind: 'financial_year', year: 1 }, available_from: 1,
    book_values: [{ year: 2, value: '80.0000' }, { year: 3, value: '60.0000' }] }],
  tax_cash: {
    vat_rules: [{ rule: 'purchase_vat', pool: 'shared_pool', month: 13, output_tax: '0', deductible_input: '10.0000' }],
    derived: [{ rule: 'surcharge', axis: 'tax_month', period: 36, assessed: '0.400000000000000001', payment_period: 38, paid: '0.400000000000000001' }],
  },
}
const before = JSON.stringify(facts)
const html = renderToStaticMarkup(createElement(I18nProvider, null, createElement(FinancialTransactions, { fin: facts })))
for (const literal of ['purchase / financial_year=0', '100.000000000000000000001 CNY', 'financial_year=4 / 0.6: 66 CNY',
  'service / tax_month=13', '-1.250000000000000001 CNY', 'tax_month=14 / 1:', 'asset_a', 'y=2: 80.0000 CNY',
  'y=3: 60.0000 CNY', 'purchase_vat', 'shared_pool', '10.0000 CNY', 'surcharge', '0.400000000000000001 CNY', '<td>38</td>']) {
  assert.ok(html.includes(literal), `exact frozen identity/period/amount retained: ${literal}`)
}
assert.ok(html.includes('期末未收付'))
assert.ok(html.includes('账面值不是残值回收现金'))
assert.ok(html.includes('期外支付保留记录'))
assert.equal(JSON.stringify(facts), before, 'render does not mutate or recalculate relation facts')
const empty = renderToStaticMarkup(createElement(I18nProvider, null, createElement(FinancialTransactions, {
  fin: { currency: 'CNY', cash_movements: [], assets: [], tax_cash: { vat_rules: [], derived: [] } },
})))
assert.equal((empty.match(/无适用规则/g) || []).length, 4)
assert.ok(!empty.includes('<table'), 'explicit non-applicability is not a zero-valued occurrence')
console.log('F2 frozen transaction rendering passed: exact decimals, occurrence/payment identity, period-0, post-horizon, assets, VAT rules and derived taxes')
