import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)
const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const { I18nProvider } = await import('../src/i18n/index.ts')
const { FinancialComparison } = await import('../src/features/results/FinancialComparison.tsx')
const fin = { currency: 'CNY', annual_net_flows: ['-10.000000000000001', '-4'], comparison: {
  mode: 'difference', baseline_source: { kind: 'task_result', task_id: 7, evidence_id: 8, object_id: 9, attempt_id: 10 },
  comparability: { service_basis: 'same demand', differences: 'explicit differences' }, facts: {},
  baseline_net_flows: ['-4', '-10'], evaluation_net_flows: ['-6.000000000000001', '6'],
} }
const before = JSON.stringify(fin)
const html = renderToStaticMarkup(createElement(I18nProvider, null, createElement(FinancialComparison, { fin })))
for (const text of ['same demand', 'explicit differences', '-10.000000000000001 CNY', '-6.000000000000001 CNY',
  '原候选税后现金流', '冻结基准税后现金流', '评价差额']) assert.ok(html.includes(text), text)
assert.equal(JSON.stringify(fin), before)
console.log('F3 comparison presentation: exact sealed decimals, distinct cashflows, no recalculation')
