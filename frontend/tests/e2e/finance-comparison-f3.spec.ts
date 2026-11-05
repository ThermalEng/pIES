// F3: explicit comparison document -> frozen task -> same-solve evaluation and export.
// Reusable physical prerequisites use public APIs; all comparison entry/submission
// and result/export actions use the real UI with no state injection or mocked solver.
import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'
import type { FinancialEvaluationBlock } from '../../src/types'

test('F3 exact sealed baseline -> comparison document -> frozen differences and Excel', async ({ browser }, testInfo) => {
  test.setTimeout(900_000)
  const username = uniqueName('f3-compare')
  const password = strongPassword('Init')
  const api = await browser.newContext({ baseURL: APP })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const pid = await scaffoldProject(requestJson, api.request, token, uniqueName('F3比较'), uniqueName('f3-profile'))
  await api.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  await createSession(page, username, password)

  async function submit(comparison = '') {
    await page.goto(`/projects/${pid}/tasks`)
    await page.getByRole('button', { name: /提交任务|Submit task/ }).first().click()
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('0')
    await page.locator('#task-operation-years').fill('1')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    await page.locator('#task-comparison').fill(comparison)
    const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith(`/projects/${pid}/tasks`))
    await page.getByRole('button', { name: /提交任务|Submit task/ }).last().click()
    const created = await response
    expect(created.status(), await created.text()).toBe(201)
    const tid = (await created.json()).task.id
    await page.locator('.ies-table__row').filter({ hasText: new RegExp(`#${tid}(?!\\d)`) }).first().click()
    await expect(async () => {
      const status = await page.locator('.ies-task-detail .ies-badge__label').first().textContent()
      expect(status).toMatch(/已完成|Completed/)
    }).toPass({ timeout: 420_000, intervals: [5000] })
    await page.getByRole('link', { name: /查看结果|View results/ }).click()
    const view = (await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${tid}/result`)).result
    expect(view.financial_evaluation).not.toBeNull()
    return { tid, view, fin: view.financial_evaluation as FinancialEvaluationBlock }
  }

  const baseline = await submit()
  expect(baseline.fin.comparison).toEqual({ mode: 'absolute' })
  const document = {
    mode: 'difference', baseline: { kind: 'task_result', task_id: baseline.tid, evidence_id: baseline.view.evidence.id },
    facts: { energy: { kind: 'operation', measure: 'energy_net' } },
    comparability: { service_basis: '同一公开合成供热负荷与承担范围', differences: '无额外差异，验证独立基准冻结' },
  }
  const candidate = await submit(JSON.stringify(document, null, 2))
  const comparison = candidate.fin.comparison
  expect(comparison.mode).toBe('difference')
  if (comparison.mode !== 'difference') throw new Error('difference not restored')
  expect(comparison.baseline_source).toMatchObject({ kind: 'task_result', task_id: baseline.tid, evidence_id: baseline.view.evidence.id })
  expect(comparison.baseline_net_flows).toEqual(baseline.fin.annual_net_flows)
  comparison.evaluation_net_flows.forEach(value => expect(Number(value)).toBeCloseTo(0, 4))
  expect(Number(candidate.fin.evaluation.npv)).toBeCloseTo(0, 4)
  const table = page.getByRole('table', { name: /冻结基准与评价口径|Frozen baseline and evaluation scope/ })
  await expect(table).toBeVisible()
  await expect(table).toContainText(candidate.fin.annual_net_flows[0])
  await expect(table).toContainText(comparison.baseline_net_flows[0])
  await expect(table).toContainText(comparison.evaluation_net_flows[0])
  await page.screenshot({ path: testInfo.outputPath('f3-sealed-comparison.png'), fullPage: true })

  await page.goto(`/projects/${pid}/export`)
  const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
  await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/ }).click()
  const file = testInfo.outputPath('f3-comparison.xlsx')
  await (await downloadPromise).saveAs(file)
  const xml = execFileSync('python3', ['-c', String.raw`
import sys,zipfile,xml.etree.ElementTree as ET
z=zipfile.ZipFile(sys.argv[1])
ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
for p in z.namelist():
  if not p.startswith('xl/worksheets/') or not p.endswith('.xml'): continue
  for row in ET.fromstring(z.read(p)).findall('.//m:sheetData/m:row',ns):
    text=[''.join(t.text or '' for t in c.findall('.//m:t',ns)) for c in row.findall('m:c',ns)]
    if text and ('Baseline CF[' in text[0] or 'Difference CF[' in text[0] or 'Comparison mode' in text[0]):
      print(' | '.join(text))
`, file], { encoding: 'utf8' })
  expect(xml).toContain('Baseline CF[t=0]')
  expect(xml).toContain('Difference CF[t=1]')
  expect(xml).toContain('difference')

  // Re-authoring uses the frozen values as declared input, not old server IDs.
  await page.goto(`/projects/${pid}/tasks`)
  await page.locator('.ies-table__row').filter({ hasText: new RegExp(`#${baseline.tid}(?!\\d)`) }).first().click()
  await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(`#${baseline.tid}`)
  await page.locator('.ies-table__row').filter({ hasText: new RegExp(`#${candidate.tid}(?!\\d)`) }).first().click()
  await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(`#${candidate.tid}`)
  await page.getByRole('button', { name: /提交任务|Submit task/ }).first().click()
  const restored = JSON.parse(await page.locator('#task-comparison').inputValue())
  expect(restored.baseline.kind).toBe('declared')
  expect(restored.baseline.annual_net_flows).toEqual(comparison.baseline_net_flows)
  expect(restored.baseline.task_id).toBeUndefined()
  const result = (await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${candidate.tid}/result`)).result
  expect(result.financial_evaluation).toEqual(candidate.fin)
  await context.close()
})
