// F1: all changed user actions use real pages. API setup only creates a base
// engineer/project/model; no route mocking, result seeding, or UI state injection.
import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, financeProfile, scaffoldProject } from './financial-scaffold'
import type { FinancialEvaluationBlock } from '../../src/types'

function excelRows(file: string, sheetName: string): unknown[][] {
  // Docker-only Python standard library: inspect exact strings in the download.
  return JSON.parse(execFileSync('python3', ['-c', String.raw`
import json, sys, zipfile, xml.etree.ElementTree as ET
z = zipfile.ZipFile(sys.argv[1])
ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
wb = ET.fromstring(z.read('xl/workbook.xml'))
relations = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
targets = {r.attrib['Id']:r.attrib['Target'] for r in relations}
sheet = next(s for s in wb.find('s:sheets', ns) if s.attrib['name'] == sys.argv[2])
target = targets[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
shared = []
if 'xl/sharedStrings.xml' in z.namelist():
  shared = [''.join(t.text or '' for t in item.findall('.//s:t', ns)) for item in ET.fromstring(z.read('xl/sharedStrings.xml'))]
rows = []
for row in ET.fromstring(z.read(sheet_path)).findall('.//s:sheetData/s:row', ns):
  if int(row.attrib['r']) < 3: continue
  values = []
  for c in row.findall('s:c', ns):
    v = c.find('s:v', ns)
    if c.attrib.get('t') == 's': value = shared[int(v.text)]
    elif c.find('s:is', ns) is not None: value = ''.join(t.text or '' for t in c.findall('.//s:t', ns))
    elif v is not None: value = int(v.text)
    else: value = None
    values.append(value)
  rows.append(values)
print(json.dumps(rows, ensure_ascii=False))
`, file, sheetName], { encoding: 'utf8' }))
}

test('F1 document -> form -> same-solve named facts -> exact Excel and frozen history', async ({ browser }, testInfo) => {
  test.setTimeout(900_000)
  const username = uniqueName('f1-named')
  const password = strongPassword('Init')
  const api = await browser.newContext({ baseURL: APP })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const pid = await scaffoldProject(requestJson, api.request, token, uniqueName('F1金额'), uniqueName('f1-base'))
  await api.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.goto(`/projects/${pid}/finance`)
  const profileId = uniqueName('f1-profile')
  const profile = financeProfile(profileId, {}, {
    heat_pump_system: {
      upfront_capex: { linear: { heat_kw: { unit_cost: { value: '2800', unit: 'CNY/kW' } } } },
      annual_fixed_om: { items: {
        materials: { linear: { heat_kw: { unit_cost: { value: '2', unit: 'CNY/kW/a' } } } },
        service: { fixed: { value: '10', unit: 'CNY/a' }, linear: { heat_kw: { unit_cost: { value: '5', unit: 'CNY/kW/a' } } } },
      } },
      period_variable_om: { linear: { consumed_kwh: { unit_cost: { value: '0.008', unit: 'CNY/kWh' } } } },
    },
  })
  profile.demand_charges = { unbound_fee: { rate: { value: '10', unit: 'CNY/kW' } } }
  profile.amounts = {
    capacity_quote: {
      axis: 'scalar', unit: 'CNY', constant: { value: '0', unit: 'CNY' },
      terms: { scale: {
        coefficient: { value: '2', unit: 'CNY/kW' },
        source: { kind: 'driver', finance_type: 'heat_pump_system', driver: 'heat_kw',
          scope: { kind: 'instances', instances: ['hp1'] }, period: { kind: 'scalar' } },
      } },
    },
  }
  await page.locator('#finance-profile-doc').fill(JSON.stringify(profile, null, 2))
  // Editing an existing convenience view must preserve the rest of the document.
  await page.locator('#profile-demand-unbound_fee').fill('11')
  const editedProfile = JSON.parse(await page.locator('#finance-profile-doc').inputValue())
  expect(editedProfile.finance_types).toEqual(profile.finance_types)
  expect(editedProfile.amounts).toEqual(profile.amounts)
  await page.getByRole('button', { name: /^登记$|^Register$/ }).click()
  await expect(page.getByText(/登记成功|Registered/).first()).toBeVisible()
  await page.locator('#finance-profile-select').selectOption(profileId)
  await page.getByRole('button', { name: /引用\/切换|Reference/ }).click()
  await expect(page.getByText(`已引用画像 ${profileId}`)).toBeVisible()

  const overrides = {
    schema: 'ies.finance-overrides', schema_version: '1.0.0', profile_ref: { id: profileId },
    finance_types: { heat_pump_system: { annual_fixed_om: { items: {
      service: { fixed: { value: '12', unit: 'CNY/a' } },
    } } } },
    amounts: { capacity_quote: { constant: { value: '0.0000000000000000000000', unit: 'CNY' } } },
    demand_charges: { unbound_fee: { rate: { value: '13', unit: 'CNY/kW' } } },
  }
  await page.locator('#finance-overrides-doc').fill(JSON.stringify(overrides, null, 2))
  await page.locator('#overrides-demand-unbound_fee').fill('14')
  const editedOverrides = JSON.parse(await page.locator('#finance-overrides-doc').inputValue())
  expect(editedOverrides.finance_types).toEqual(overrides.finance_types)
  expect(editedOverrides.amounts).toEqual(overrides.amounts)
  await page.getByRole('button', { name: /保存覆盖|Save overrides/ }).click()
  await expect(page.getByText(/覆盖已保存|Overrides saved/).first()).toBeVisible()
  await page.reload()
  const savedOverrides = JSON.parse(await page.locator('#finance-overrides-doc').inputValue())
  expect(savedOverrides.finance_types.heat_pump_system.annual_fixed_om.items.service.fixed.value).toBe('12')
  expect(savedOverrides.amounts.capacity_quote.constant.value).toBe('0.0000000000000000000000')
  const effective = JSON.parse(await page.locator('pre.ies-pre').last().textContent() ?? '')
  expect(effective.finance_types.heat_pump_system.annual_fixed_om.items.service.fixed.value).toBe('12')
  expect(effective.amounts.capacity_quote.terms.scale.source.scope.instances).toEqual(['hp1'])

  await page.goto(`/projects/${pid}/planning`)
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  const planning = {
    objective: { sense: 'minimize', expression: 'grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om' },
    variables: { hp_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW' } },
    constraints: { minimum_quote: { type: 'amount_relation', enabled: true,
      references: { quote: { kind: 'amount', id: 'capacity_quote', period: { kind: 'scalar' } } },
      expression: 'quote >= 120 CNY',
    } },
  }
  await page.locator('#planning-yaml').fill(JSON.stringify(planning, null, 2))
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.locator('[id^="planning-con-type-"]')).toHaveValue('amount_relation')
  await page.locator('[id^="planning-var-upper-"]').fill('900')
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.getByText(/规划配置已保存 revision|Planning config saved/)).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  const savedPlanning = await requestJson(page.request, 'GET', `/api/projects/${pid}/planning-config`)
  expect(savedPlanning.planning_config.constraints.minimum_quote).toEqual(planning.constraints.minimum_quote)
  expect(await page.locator('#planning-yaml').inputValue()).toContain('capacity_quote')

  await page.goto(`/projects/${pid}/versions`)
  await page.getByRole('button', { name: /创建版本|Create version/ }).first().click()
  await page.locator('#version-name').fill('F1 named amount frozen')
  await page.getByRole('button', { name: /^创建$|^Create$/ }).click()
  await expect(page.getByText('F1 named amount frozen')).toBeVisible()
  await page.goto(`/projects/${pid}/tasks`)
  await page.getByRole('button', { name: /提交任务|Submit task/ }).first().click()
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('0')
  await page.locator('#task-operation-years').fill('1')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  const submission = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`/api/projects/${pid}/tasks`))
  await page.getByRole('button', { name: /提交任务|Submit task/ }).last().click()
  const submitted = await submission
  expect(submitted.status(), await submitted.text()).toBe(201)
  await page.locator('.ies-table__row').first().click()
  await expect(async () => expect(await page.locator('.ies-task-detail .ies-badge__label').first().textContent()).toMatch(/已完成|Completed/))
    .toPass({ timeout: 420_000, intervals: [10_000] })
  await page.getByRole('link', { name: /查看结果|View results/ }).click()
  const tasks = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks?limit=10`)
  const taskId = tasks.items[0].id
  const result = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
  const fin = result.result.financial_evaluation as FinancialEvaluationBlock
  const quote = fin.amounts.find(amount => amount.id === 'capacity_quote')!
  expect(quote.axis).toBe('scalar')
  expect(quote.unit).toBe('CNY')
  expect(Number(quote.values[0].value)).toBeCloseTo(120, 6)
  const items = fin.cost_items.filter(item => item.instance_id === 'hp1' && item.component === 'annual_fixed_om' && item.axis === 'scalar')
  expect(items.map(item => item.item_id).sort()).toEqual(['materials', 'service'])
  expect(Number(items.find(item => item.item_id === 'materials')!.raw)).toBeCloseTo(120, 6)
  expect(Number(items.find(item => item.item_id === 'service')!.raw)).toBeCloseTo(312, 6)
  expect(Number(fin.annual_operation[0].fixed_om)).toBeCloseTo(432, 6)
  const costTable = page.getByRole('table', { name: /命名成本明细|Named cost items/ })
  for (const item of items) {
    const row = costTable.locator('tbody tr').filter({ hasText: `${item.finance_type} / ${item.component} / ${item.item_id}` }).filter({ hasText: item.unit })
    await expect(row.first()).toContainText(item.raw)
    await expect(row.first()).toContainText(item.net_basis)
  }
  await expect(page.getByRole('table', { name: /命名金额|Named amounts/ })).toContainText(quote.values[0].value)
  await page.screenshot({ path: testInfo.outputPath('f1-named-facts.png'), fullPage: true })

  // Change only the current document. Historical result and report stay frozen.
  await page.goto(`/projects/${pid}/finance`)
  savedOverrides.finance_types.heat_pump_system.annual_fixed_om.items.service.fixed.value = '999'
  await page.locator('#finance-overrides-doc').fill(JSON.stringify(savedOverrides))
  await page.getByRole('button', { name: /保存覆盖|Save overrides/ }).click()
  await expect(page.getByText(/覆盖已保存|Overrides saved/).first()).toBeVisible()
  const oldResult = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
  expect(oldResult.result.financial_evaluation).toEqual(fin)
  await page.goto(`/projects/${pid}/export`)
  const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
  await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/ }).click()
  const download = await downloadPromise
  const file = testInfo.outputPath('f1-named-report.xlsx')
  await download.saveAs(file)
  expect(fs.statSync(file).size).toBeGreaterThan(0)
  const expectedCosts = fin.cost_items.map(item => [item.instance_id, item.finance_type, item.component, item.item_id, item.axis, item.period,
    item.raw, item.gross, item.deductible_input, item.net_basis, item.unit])
  const expectedAmounts = fin.amounts.flatMap(amount => amount.values.map(value => [amount.id, amount.axis, value.period,
    /^[=+@-]/.test(value.value) ? "'" + value.value : value.value, amount.unit]))
  expect(excelRows(file, '命名成本明细')).toEqual(expectedCosts)
  expect(excelRows(file, '命名金额')).toEqual(expectedAmounts)
  expect(path.extname(download.suggestedFilename())).toBe('.xlsx')
  // A fresh project has no saved planning revision until the first UI save.
  session.assertNoErrors([{ method: 'GET', path: `/api/projects/${pid}/planning-config`, status: 404, times: 1 }])
  await context.close()
})
