import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)
const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const { I18nProvider } = await import('../src/i18n/index.ts')
const { FinancialRelations } = await import('../src/features/results/FinancialRelations.tsx')

const facts = {
  costItems: [
    { instance_id: 'unit_a', finance_type: 'shared', component: 'annual_fixed_om', item_id: 'service', axis: 'scalar', period: 0,
      raw: '25.0000000000000000000001', gross: '27.50', deductible_input: '2.50', net_basis: '25.0000', unit: 'CNY/a' },
    { instance_id: 'unit_b', finance_type: 'shared', component: 'annual_fixed_om', item_id: 'service', axis: 'financial_year', period: 2,
      raw: '0', gross: '0', deductible_input: '0', net_basis: '0', unit: 'CNY' },
  ],
  amounts: [
    { id: 'signed_scalar', axis: 'scalar', unit: 'CNY', values: [{ period: 0, value: '-4.5000000000000000000001' }] },
    { id: 'year_rate', axis: 'financial_year', unit: 'CNY/a', values: [{ period: 2, value: '31' }, { period: 3, value: '0' }] },
    { id: 'monthly_value', axis: 'tax_month', unit: 'CNY', values: [{ period: 13, value: '8.1250' }] },
  ],
}
const before = JSON.stringify(facts)
const html = renderToStaticMarkup(createElement(I18nProvider, null, createElement(FinancialRelations, facts)))
for (const value of ['unit_a', 'unit_b', 'shared / annual_fixed_om / service', '25.0000000000000000000001', '25.0000', '-4.5000000000000000000001', '8.1250', 'scalar', 'financial_year', 'tax_month', 'CNY/a']) {
  assert.ok(html.includes(value), `renders frozen identity/value/unit: ${value}`)
}
assert.match(html, /命名金额仅表示数值关系，不额外计入现金流/)
assert.match(html, /<td>3<\/td><td>0<\/td><td>CNY\/a<\/td>/)
assert.equal(JSON.stringify(facts), before, 'presentation does not mutate/recompute frozen facts')
const empty = renderToStaticMarkup(createElement(I18nProvider, null, createElement(FinancialRelations, { costItems: [], amounts: [] })))
assert.match(empty, /无适用命名成本项/)
assert.match(empty, /无适用命名金额/)
assert.ok(!empty.includes('<table'), 'not applicable is not a fabricated zero row')
console.log('Frozen named cost/amount rendering: exact decimals, signed/zero values, identity, axis, period, units, and explicit not-applicable passed')
