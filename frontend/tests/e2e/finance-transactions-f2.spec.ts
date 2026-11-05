// F2 public synthetic arithmetic. Only reusable project/model prerequisites use
// API setup; authoring, overrides, planning, freeze, submission and export use UI.
import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, financeProfile, scaffoldProject } from './financial-scaffold'
import type { FinancialEvaluationBlock } from '../../src/types'

function workbookRows(file: string, sheet: string): unknown[][] {
  return JSON.parse(execFileSync('python3', ['-c', String.raw`
import json, sys, zipfile, xml.etree.ElementTree as ET
z=zipfile.ZipFile(sys.argv[1]); ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
wb=ET.fromstring(z.read('xl/workbook.xml')); rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
targets={r.attrib['Id']:r.attrib['Target'] for r in rels}
s=next(s for s in wb.find('m:sheets',ns) if s.attrib['name']==sys.argv[2])
p=targets[s.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
p=p.lstrip('/') if p.startswith('/') else 'xl/'+p
shared=[]
if 'xl/sharedStrings.xml' in z.namelist():
  shared=[''.join(t.text or '' for t in v.findall('.//m:t',ns)) for v in ET.fromstring(z.read('xl/sharedStrings.xml'))]
out=[]
for r in ET.fromstring(z.read(p)).findall('.//m:sheetData/m:row',ns):
  if int(r.attrib['r'])<3: continue
  row=[]
  for c in r.findall('m:c',ns):
    v=c.find('m:v',ns); inline=c.find('m:is',ns)
    if c.attrib.get('t')=='s': value=shared[int(v.text)]
    elif inline is not None: value=''.join(t.text or '' for t in inline.findall('.//m:t',ns))
    elif v is not None: value=int(v.text)
    else: value=None
    row.append(value)
  out.append(row)
print(json.dumps(out,ensure_ascii=False))
`, file, sheet], { encoding: 'utf8' }))
}

test('F2 documents -> same-solve payment/asset/tax facts -> Excel and frozen history', async ({ browser }, testInfo) => {
  test.setTimeout(900_000)
  const username = uniqueName('f2-cash')
  const password = strongPassword('Init')
  const api = await browser.newContext({ baseURL: APP })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const pid = await scaffoldProject(requestJson, api.request, token, uniqueName('F2关系'), uniqueName('f2-base'))
  await api.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  const profileId = uniqueName('f2-profile')
  const profile = financeProfile(profileId)
  profile.amounts = Object.fromEntries([
    ['purchase_quote', '100'], ['service_quote', '100'], ['revenue_quote', '1000'], ['net_receipt', '5'],
  ].map(([id, value]) => [id, { unit: 'CNY', axis: 'scalar', constant: { value, unit: 'CNY' }, terms: {} }]))
  profile.cash_movements = {
    purchase: { amount: { kind: 'amount', id: 'purchase_quote', period: { kind: 'scalar' } },
      direction: 'outflow', price_basis: 'tax_inclusive',
      recognition: { kind: 'at', at: { kind: 'financial_year', year: 0 } },
      payments: [{ offset: 0, ratio: '0.4' }, { offset: 2, ratio: '0.6' }] },
    service: { amount: { kind: 'amount', id: 'service_quote', period: { kind: 'scalar' } },
      direction: 'outflow', price_basis: 'tax_exclusive',
      recognition: { kind: 'each', axis: 'financial_year', first: 1, last: 2 },
      payments: [{ offset: 0, ratio: '1' }] },
    revenue: { amount: { kind: 'amount', id: 'revenue_quote', period: { kind: 'scalar' } },
      direction: 'inflow', price_basis: 'tax_exclusive',
      recognition: { kind: 'each', axis: 'financial_year', first: 1, last: 3 },
      payments: [{ offset: 0, ratio: '1' }] },
  }
  profile.assets = { asset_a: {
    basis_from: { kind: 'movement', id: 'purchase', basis: 'net_basis', period: { kind: 'financial_year', year: 0 } },
    capitalized_at: { kind: 'financial_year', year: 1 }, available_from: 1,
  } }
  profile.credit_pools = { shared: { opening_credit: { value: '0', unit: 'CNY' }, receivable_policy: 'end_receivable', refunds: [] } }
  profile.taxes = {
    vat_label: { display_name: '合成交易税', tax_type: 'value_added_tax', applies_to: 'grid_import' },
    income_label: { display_name: '合成所得税', tax_type: 'income_tax', applies_to: 'grid_import' },
    derived_label: { display_name: '合成派生费', tax_type: 'synthetic_levy', applies_to: 'grid_import' },
  }
  profile.tax_rules = {
    transaction_vat: { kind: 'vat', label_ref: 'vat_label', settlement: 'monthly', credit_pool: 'shared',
      confirmation: 'same_period', in_tax: [], out_tax: [], om_input_tax: [], maintenance_input_tax: [],
      movement_input_tax: [{ movement: 'service', rate: { value: '0.1', unit: '1' }, deductible_ratio: { value: '1', unit: '1' },
        confirmation: { method: 'explicit_schedule', occurrences: [
          { at: { kind: 'financial_year', year: 1 }, schedule: [{ period: 13, ratio: '1' }] },
          { at: { kind: 'financial_year', year: 2 }, schedule: [{ period: 25, ratio: '1' }] },
        ] } }],
      movement_output_tax: [{ movement: 'revenue', rate: { value: '0.1', unit: '1' }, confirmation: { method: 'recognition_period' } }],
    },
    levy: { kind: 'derived_tax', label_ref: 'derived_label', axis: 'tax_month',
      base_from: { kind: 'tax', source: { kind: 'pool', id: 'shared' }, measure: 'paid', period: { kind: 'current' } },
      rate: { value: '0.04', unit: '1' }, payment_lag: 2 },
    income: { kind: 'income_tax', label_ref: 'income_label', rate: { value: '0.25', unit: '1' },
      revenue: [], energy_cost: [], annual_om: [], variable_om: [],
      deductible_maintenance: { mode: 'explicit_items', items: [] },
      depreciation: [{ base_from: { kind: 'asset', id: 'asset_a' }, method: 'straight_line',
        life_years: 5, first_year: 1, residual: { value: '0', unit: 'CNY' } }],
      loss_carryforward_years: 0, opening_losses: [],
      movement_revenue: ['revenue'], movement_cost: ['service'], derived_tax_cost: ['levy'],
    },
  }
  profile.net_cashflows = { explicit_receipt: { at: { kind: 'evaluation_end' }, direction: 'inflow', amount: { kind: 'amount_ref', id: 'net_receipt' } } }
  await page.goto(`/projects/${pid}/finance`)
  await page.locator('#finance-profile-doc').fill(JSON.stringify(profile, null, 2))
  await page.getByRole('button', { name: /^登记$|^Register$/ }).click()
  await expect(page.getByText(/登记成功|Registered/).first()).toBeVisible()
  await page.locator('#finance-profile-select').selectOption(profileId)
  await page.getByRole('button', { name: /引用\/切换|Reference/ }).click()
  await expect(page.getByText(`已引用画像 ${profileId}`)).toBeVisible()
  // Overrides exercise complete schedule replacement without replacing IDs/methods.
  const overrides = { schema: 'ies.finance-overrides', schema_version: '1.0.0', profile_ref: { id: profileId },
    cash_movements: { purchase: { payments: [{ offset: 0, ratio: '0.4' }, { offset: 2, ratio: '0.6' }] } },
    assets: { asset_a: { capitalized_at: { kind: 'financial_year', year: 1 }, available_from: 1 } },
    tax_rules: { levy: { rate: { value: '0.04', unit: '1' }, payment_lag: 2 } },
  }
  await page.locator('#finance-overrides-doc').fill(JSON.stringify(overrides, null, 2))
  await page.getByRole('button', { name: /保存覆盖|Save overrides/ }).click()
  await expect(page.getByText(/覆盖已保存|Overrides saved/).first()).toBeVisible()
  await page.reload()
  const savedOverrides = JSON.parse(await page.locator('#finance-overrides-doc').inputValue())
  expect(savedOverrides.cash_movements.purchase.payments).toEqual(overrides.cash_movements.purchase.payments)
  const effective = JSON.parse(await page.locator('pre.ies-pre').last().textContent() ?? '')
  expect(effective.cash_movements.purchase.payments).toEqual(overrides.cash_movements.purchase.payments)
  expect(effective.assets.asset_a).toEqual((profile.assets as Record<string, unknown>).asset_a)
  expect(effective.tax_rules.transaction_vat.movement_input_tax[0].confirmation.occurrences).toHaveLength(2)

  await page.goto(`/projects/${pid}/planning`)
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  await page.locator('#planning-yaml').fill(JSON.stringify({
    objective: { sense: 'maximize', expression: 'system.after_tax_npv' },
    variables: { hp_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW' } }, constraints: {},
  }))
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.getByText(/规划配置已保存 revision|Planning config saved/)).toBeVisible()
  await page.goto(`/projects/${pid}/versions`)
  await page.getByRole('button', { name: /创建版本|Create version/ }).first().click()
  await page.locator('#version-name').fill('F2 frozen relations')
  await page.getByRole('button', { name: /^创建$|^Create$/ }).click()
  await expect(page.getByText('F2 frozen relations')).toBeVisible()
  await page.goto(`/projects/${pid}/tasks`)
  await page.getByRole('button', { name: /提交任务|Submit task/ }).first().click()
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t":1,"ratio":1}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  await page.getByRole('button', { name: /提交任务|Submit task/ }).last().click()
  await page.locator('.ies-table__row').first().click()
  await expect(async () => expect(await page.locator('.ies-task-detail .ies-badge__label').first().textContent()).toMatch(/已完成|Completed/))
    .toPass({ timeout: 420_000, intervals: [10_000] })
  await page.getByRole('link', { name: /查看结果|View results/ }).click()
  const tasks = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks?limit=10`)
  const taskId = tasks.items[0].id
  const fetchResult = () => requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
  const fin = (await fetchResult()).result.financial_evaluation as FinancialEvaluationBlock
  const purchase = fin.cash_movements.find(item => item.id === 'purchase')!
  expect(purchase).toMatchObject({ axis: 'financial_year', period: 0, direction: 'outflow' })
  expect(purchase.payments.map(p => p.at)).toEqual([{ kind: 'financial_year', year: 0 }, { kind: 'financial_year', year: 2 }])
  expect(purchase.payments.map(p => Number(p.amount))).toEqual([40, 60])
  const service = fin.cash_movements.filter(item => item.id === 'service')
  expect(service.map(item => item.period)).toEqual([1, 2])
  for (const row of service) {
    expect(Number(row.gross)).toBeCloseTo(110, 7)
    expect(Number(row.net_basis)).toBeCloseTo(100, 7)
  }
  expect(Number(fin.assets[0].basis)).toBeCloseTo(100, 7)
  expect(fin.assets[0]).toMatchObject({ id: 'asset_a', capitalized_at: { kind: 'financial_year', year: 1 }, available_from: 1 })
  expect(Number(fin.assets[0].book_values.find(row => row.year === 2)!.value)).toBeCloseTo(80, 7)
  expect(Number(fin.assets[0].book_values.find(row => row.year === 3)!.value)).toBeCloseTo(60, 7)
  expect(fin.tax_cash.depreciation.filter(row => row.base_from.kind === 'asset').map(row => Number(row.amount))).toEqual([20, 20])
  for (const [month, paid] of [[12, 100], [24, 90], [36, 90]]) {
    expect(Number(fin.tax_cash.vat.find(row => row.pool === 'shared' && row.month === month)!.paid)).toBeCloseTo(paid, 6)
  }
  for (const [period, paymentPeriod, expected] of [[12, 14, 4], [24, 26, 3.6], [36, 38, 3.6]]) {
    const row = fin.tax_cash.derived.find(row => row.rule === 'levy' && row.period === period)!
    expect(row.payment_period).toBe(paymentPeriod)
    expect(Number(row.assessed)).toBeCloseTo(expected, 6)
    expect(Number(row.paid)).toBeCloseTo(expected, 6)
  }
  for (const [year, expected] of [[1, 224], [2, 219.1], [3, 244.1]]) {
    expect(Number(fin.tax_cash.income.find(row => row.year === year)!.assessed)).toBeCloseTo(expected, 6)
  }
  const movementTable = page.getByRole('table', { name: /具名收付|Cash movements/ })
  await expect(movementTable.locator('tr', { hasText: 'purchase / financial_year=0' })).toContainText(purchase.gross)
  const assetTable = page.getByRole('table', { name: /资产及账面值|Assets and book values/ })
  await expect(assetTable).toContainText(fin.assets[0].basis)
  const derivedTable = page.getByRole('table', { name: /派生税费|Derived taxes/ })
  await expect(derivedTable).toContainText('38')
  await expect(page.getByRole('table', { name: /规则级增值税确认|Rule-level VAT/ })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('f2-frozen-transactions.png'), fullPage: true })

  // Change a current payment plan but not the old task's occurrence or payment facts.
  await page.goto(`/projects/${pid}/finance`)
  savedOverrides.cash_movements.purchase.payments = [{ offset: 0, ratio: '1' }]
  await page.locator('#finance-overrides-doc').fill(JSON.stringify(savedOverrides))
  await page.getByRole('button', { name: /保存覆盖|Save overrides/ }).click()
  await expect(page.getByText(/覆盖已保存|Overrides saved/).first()).toBeVisible()
  expect((await fetchResult()).result.financial_evaluation).toEqual(fin)
  await page.goto(`/projects/${pid}/export`)
  const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
  await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/ }).click()
  const download = await downloadPromise
  const file = testInfo.outputPath('f2-frozen-report.xlsx')
  await download.saveAs(file)
  const safe = (text: string) => /^[=+@-]/.test(text) ? "'" + text : text
  expect(workbookRows(file, '收付发生项')).toEqual(fin.cash_movements.map(row => [row.id, row.axis, row.period, row.direction, row.price_basis,
    row.raw, row.gross, row.deductible_input, row.net_basis, row.output_tax, row.net_revenue, row.end_unpaid, fin.currency].map(v => typeof v === 'string' ? safe(v) : v)))
  expect(workbookRows(file, '收付支付计划')).toEqual(fin.cash_movements.flatMap(row => row.payments.map((p, index) => [row.id, row.axis, row.period,
    index + 1, p.at.kind, p.at.kind === 'financial_year' ? p.at.year : p.at.month, p.ratio, safe(p.amount), fin.currency])))
  expect(workbookRows(file, '资产基础')).toEqual(fin.assets.map(a => [a.id, a.basis, a.capitalized_at.kind,
    a.capitalized_at.kind === 'financial_year' ? a.capitalized_at.year : a.capitalized_at.month, a.available_from, fin.currency]))
  expect(workbookRows(file, '资产账面值')).toEqual(fin.assets.flatMap(a => a.book_values.map(v => [a.id, v.year, v.value, fin.currency])))
  expect(workbookRows(file, '规则级增值税')).toEqual(fin.tax_cash.vat_rules.map(r => [r.rule, r.pool, r.month, safe(r.output_tax), safe(r.deductible_input), fin.currency]))
  expect(workbookRows(file, '派生税费')).toEqual(fin.tax_cash.derived.map(r => [r.rule, r.axis, r.period, safe(r.assessed), r.payment_period, safe(r.paid), fin.currency]))
  expect((await fetchResult()).result.financial_evaluation).toEqual(fin)
  session.assertNoErrors([
    { method: 'GET', path: `/api/projects/${pid}/planning-config`, status: 404, times: 1 },
  ])
  await context.close()
})
