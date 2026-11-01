/** D3 browser acceptance: rate override + explicit meter CRUD + frozen solve/report.
 * Setup uses public API for the unrelated pre-existing model and planning inputs.
 * All D3 editing, save, refresh, task submit, result reading, and export actions
 * use the real page in Docker Playwright; no page state injection or mocks.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import zlib from 'node:zlib'
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, financeProfile, scaffoldProject } from './financial-scaffold'

/** Read/prepare data using the active browser session; API setup bearer tokens
 * are revoked when the same user logs into the real GUI. Never re-login during
 * GUI acceptance, as doing so would invalidate the GUI session instead. */
async function browserJson<T>(page: Page, method: 'GET' | 'POST' | 'PUT', url: string, body?: unknown): Promise<T> {
  return page.evaluate(async ({ method, url, body }) => {
    const response = await fetch(url, {
      method,
      credentials: 'include',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`${method} ${url} -> ${response.status}: ${text.slice(0, 500)}`)
    return (text ? JSON.parse(text) : null) as T
  }, { method, url, body })
}

type Month = {
  billing_id: string; charge_id: string; calendar_year: number; calendar_month: number
  financial_year: number; peak_power: string; peak_unit: string
  rate: { value: string; unit: string }; charge: string
}

function workbookXml(bytes: Buffer): string {
  const parts: string[] = []
  let offset = 0
  while (offset + 30 <= bytes.length && bytes.readUInt32LE(offset) === 0x04034b50) {
    const compression = bytes.readUInt16LE(offset + 8)
    const size = bytes.readUInt32LE(offset + 18)
    const dataOffset = offset + 30 + bytes.readUInt16LE(offset + 26) + bytes.readUInt16LE(offset + 28)
    const data = bytes.subarray(dataOffset, dataOffset + size)
    parts.push((compression === 8 ? zlib.inflateRawSync(data) : data).toString('utf8'))
    offset = dataOffset + size
  }
  return parts.join('\n')
}

test.describe('D3 natural-month demand GUI → task → export', () => {
  test.setTimeout(900_000)

  test('profile catalogue add/edit/delete and register through existing finance editor', async ({ browser }) => {
    const userName = uniqueName('d3-profile-user')
    const password = strongPassword('Init')
    const api = await browser.newContext({ baseURL: APP })
    await createEngineer(api.request, userName, password)
    const token = await loginToken(api.request, userName, password)
    const created = await requestJson(api.request, 'POST', '/api/projects', {
      name: uniqueName('D3 费率画像'), baseline_resolution: '1h',
      baseline_start_year: 2025, baseline_scenario_mode: 'single',
    }, token)
    const pid: number = created.project.id
    const profileId = uniqueName('d3-rate-profile')
    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, userName, password)
    await page.goto(`${APP}/projects/${pid}/finance`)
    await page.getByRole('textbox', { name: /画像 JSON 文档|Profile JSON document/i }).fill(JSON.stringify(financeProfile(profileId)))
    await page.getByRole('textbox', { name: /新费项 ID|New charge ID/i }).fill('peak_charge')
    await page.getByRole('textbox', { name: /月需量费率|Monthly demand rate/i }).fill('8')
    await page.getByRole('button', { name: /添加费项|Add charge/i }).click()
    await page.getByRole('textbox', { name: 'peak_charge (CNY/kW)' }).fill('18')
    await page.getByRole('button', { name: /删除费项|Delete charge/i }).click()
    await expect(page.getByRole('textbox', { name: 'peak_charge (CNY/kW)' })).toHaveCount(0)
    await page.getByRole('textbox', { name: /新费项 ID|New charge ID/i }).fill('peak_charge')
    await page.getByRole('textbox', { name: /月需量费率|Monthly demand rate/i }).fill('18')
    await page.getByRole('button', { name: /添加费项|Add charge/i }).click()
    await page.getByRole('button', { name: /^登记$|^Register$/i }).click()
    await expect(page.getByText(/登记成功|Registered and added/i)).toBeVisible()
    await page.getByRole('combobox', { name: /选择已登记画像|Select a registered profile/i }).selectOption(profileId)
    await page.getByRole('button', { name: /引用\/切换|Reference \/ switch/i }).click()
    await page.reload()
    await expect(page.getByText(new RegExp(`当前引用|Current reference`))).toBeVisible()
    const bound = await browserJson<{ finance_profile: { demand_charges: unknown } }>(page, 'GET', `/api/projects/${pid}/finance-profile`)
    expect(bound.finance_profile.demand_charges).toEqual({ peak_charge: { rate: { value: '18', unit: 'CNY/kW' } } })
    // The unbound finance page legitimately probes three absent resources
    // before the profile is registered/bound. Only these exact GET 404s are
    // expected; unrelated console errors and network failures still fail.
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}/finance-profile`, status: 404 },
      { method: 'GET', path: `/api/projects/${pid}/finance-overrides`, status: 404 },
      { method: 'GET', path: `/api/projects/${pid}/effective-finance`, status: 404 },
    ])
    await context.close()
    await api.close()
  })


  test('overrides save/readback and profile switch lock both editors; 409 keeps edits until explicit reload', async ({ browser }) => {
    const username = uniqueName('d3-lock')
    const password = strongPassword('Init')
    const api = await browser.newContext({ baseURL: APP })
    await createEngineer(api.request, username, password)
    const token = await loginToken(api.request, username, password)
    const firstId = uniqueName('d3-lock-first')
    const secondId = uniqueName('d3-lock-second')
    const firstProfile = financeProfile(firstId)
    firstProfile.demand_charges = { peak_fee: { rate: { value: '12', unit: 'CNY/kW' } } }
    const secondProfile = financeProfile(secondId)
    secondProfile.demand_charges = { peak_fee: { rate: { value: '6', unit: 'CNY/kW' } } }
    for (const profile of [firstProfile, secondProfile]) {
      await requestJson(api.request, 'POST', '/api/finance-profiles', { finance_profile: profile }, token)
    }
    const created = await requestJson(api.request, 'POST', '/api/projects', {
      name: uniqueName('D3 在途覆盖'), baseline_resolution: '1h',
      baseline_start_year: 2025, baseline_scenario_mode: 'single',
    }, token)
    const pid: number = created.project.id
    await requestJson(api.request, 'PUT', `/api/projects/${pid}/finance-profile`, {
      profile_ref: { id: firstId },
    }, token)

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.goto(`${APP}/projects/${pid}/finance`)
    const overridesPath = `/api/projects/${pid}/finance-overrides`
    const rate = page.getByRole('textbox', { name: 'peak_fee (CNY/kW)' })
    const raw = page.locator('#finance-overrides-doc')
    const save = page.getByRole('button', { name: /保存覆盖|Save overrides/i })
    const clear = page.getByRole('button', { name: /清空覆盖|Clear overrides/i })
    const revert = page.getByRole('button', { name: /撤销项目覆盖|Revert override/i })
    const select = page.locator('#finance-profile-select')
    const bind = page.getByRole('button', { name: /引用|Reference/i })
    await expect(rate).toBeEnabled()

    // Starting from an empty Overrides editor must author a 7.0.0 envelope.
    await raw.fill('')
    await rate.fill('20')
    const authoredRate = JSON.parse(await raw.inputValue())
    expect(authoredRate.schema).toBe('ies.finance-overrides')
    expect(authoredRate.schema_version).toBe('1.0.0')
    expect(authoredRate.profile_ref).toEqual({ id: firstId })
    // Make a real first GUI save, so the clear action has an active revision.
    await save.click()
    await expect(page.getByText(/覆盖已保存|Overrides saved/i)).toBeVisible()
    await expect(rate).toHaveValue('20')
    await expect(clear).toBeEnabled()

    // Withhold the actual PUT, then the actual GET re-read separately. Nothing
    // is mocked or fabricated: both requests continue to the real backend.
    let releasePut!: () => void
    let releaseRead!: () => void
    const heldPut = new Promise<void>((resolve) => { releasePut = resolve })
    const heldRead = new Promise<void>((resolve) => { releaseRead = resolve })
    let putEntered = false
    let readEntered = false
    let holdNextRead = false
    await page.route(`**${overridesPath}`, async (route) => {
      if (route.request().method() === 'PUT') {
        putEntered = true
        await heldPut
        holdNextRead = true
      } else if (route.request().method() === 'GET' && holdNextRead) {
        readEntered = true
        holdNextRead = false
        await heldRead
      }
      await route.continue()
    })
    await rate.fill('30')
    await save.click()
    await expect.poll(() => putEntered).toBe(true)
    await expect(rate).toBeDisabled()
    await expect(raw).toBeDisabled()
    await expect(revert).toBeDisabled()
    await expect(clear).toBeDisabled()
    await expect(select).toBeDisabled()
    await expect(bind).toBeDisabled()
    expect(JSON.parse(await raw.inputValue()).demand_charges.peak_fee.rate.value).toBe('30')

    releasePut()
    await expect.poll(() => readEntered).toBe(true)
    await expect(rate).toBeDisabled()
    await expect(raw).toBeDisabled()
    await expect(revert).toBeDisabled()
    await expect(select).toBeDisabled()
    releaseRead()
    await expect(save).toBeEnabled()
    await expect(rate).toHaveValue('30')
    await expect(raw).toBeEnabled()
    await expect(revert).toBeEnabled()
    await expect(select).toBeEnabled()
    await page.unroute(`**${overridesPath}`)
    const saved = await browserJson<{ revision: number; finance_overrides: Record<string, unknown> }>(
      page, 'GET', overridesPath,
    )
    expect((saved.finance_overrides.demand_charges as { peak_fee: { rate: { value: string } } }).peak_fee.rate.value).toBe('30')

    // A concurrent update creates a real 409. UI retains the unsaved edit and
    // reloads the newer backend document ONLY on explicit user action.
    await rate.fill('40')
    await browserJson(page, 'PUT', overridesPath, {
      finance_overrides: {
        ...saved.finance_overrides,
        demand_charges: { peak_fee: { rate: { value: '35', unit: 'CNY/kW' } } },
      },
      expected_revision: saved.revision,
    })
    await save.click()
    const reload = page.getByRole('button', { name: /版本冲突.*重新加载|Version conflict, reload/i })
    await expect(reload).toBeVisible()
    await expect(rate).toHaveValue('40')
    expect(JSON.parse(await raw.inputValue()).demand_charges.peak_fee.rate.value).toBe('40')
    await reload.click()
    await expect(rate).toHaveValue('35')

    // Profile switch also replaces this same document. Lock both edit paths
    // until the switch PUT AND its ensuing GET re-read complete.
    let releaseBind!: () => void
    let releaseSwitchRead!: () => void
    const heldBind = new Promise<void>((resolve) => { releaseBind = resolve })
    const heldSwitchRead = new Promise<void>((resolve) => { releaseSwitchRead = resolve })
    let bindEntered = false
    let switchReadEntered = false
    let armSwitchRead = false
    await page.route(`**/api/projects/${pid}/finance-profile`, async (route) => {
      if (route.request().method() === 'PUT') {
        bindEntered = true
        await heldBind
        armSwitchRead = true
      }
      await route.continue()
    })
    await page.route(`**${overridesPath}`, async (route) => {
      if (route.request().method() === 'GET' && armSwitchRead) {
        switchReadEntered = true
        armSwitchRead = false
        await heldSwitchRead
      }
      await route.continue()
    })
    await select.selectOption(secondId)
    await bind.click()
    await expect.poll(() => bindEntered).toBe(true)
    await expect(rate).toBeDisabled()
    await expect(raw).toBeDisabled()
    await expect(revert).toBeDisabled()
    await expect(select).toBeDisabled()
    await expect(save).toBeDisabled()
    releaseBind()
    await expect.poll(() => switchReadEntered).toBe(true)
    await expect(rate).toBeDisabled()
    await expect(raw).toBeDisabled()
    await expect(revert).toBeDisabled()
    releaseSwitchRead()
    await expect(page.locator('dd').filter({ hasText: secondId }).first()).toBeVisible()
    await expect(rate).toBeEnabled()
    await expect(raw).toBeEnabled()
    await expect(select).toBeEnabled()
    await expect(bind).toBeEnabled()

    // A real revision conflict is the only expected non-successful write.
    // Switches may produce the legitimate no-overrides GET 404.
    session.assertNoErrors([
      { method: 'PUT', path: overridesPath, status: 409, times: 1 },
      { method: 'GET', path: overridesPath, status: 404 },
    ])
    await context.close()
    await api.close()
  })

  test('structured rate/explicit meters persist and completed task publishes identical bills', async ({ browser }) => {
    const name = uniqueName('d3-demand')
    const password = strongPassword('Init')
    const api = await browser.newContext({ baseURL: APP })
    await createEngineer(api.request, name, password)
    const token = await loginToken(api.request, name, password)
    const profileId = uniqueName('d3-profile')
    const pid = await scaffoldProject(requestJson, api.request, token, uniqueName('D3 月需量项目'), profileId, {
      demandCharges: {
        peak_fee: { rate: { value: '12', unit: 'CNY/kW' } },
        zero_fee: { rate: { value: '0', unit: 'CNY/kW' } },
      },
    })
    const originalPlanning = await requestJson(api.request, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)

    const context = await browser.newContext({ acceptDownloads: true })
    const page = await context.newPage()
    const session = await createSession(page, name, password)

    // A project override only replaces an existing Profile charge, never creates
    // a second tariff catalogue or changes the registered Profile.
    await page.goto(`${APP}/projects/${pid}/finance`)
    const rate = page.getByRole('textbox', { name: 'peak_fee (CNY/kW)' })
    await expect(rate).toBeVisible()
    await rate.fill('20')
    await page.getByRole('button', { name: /保存覆盖|Save overrides/i }).click()
    await expect(page.getByText(/覆盖已保存|Overrides saved/i)).toBeVisible()
    await page.reload()
    await expect(page.getByRole('textbox', { name: 'peak_fee (CNY/kW)' })).toHaveValue('20')
    const effective = await browserJson<{ effective_finance_config: { demand_charges: Record<string, { rate: { value: string; unit: string } }> } }>(page, 'GET', `/api/projects/${pid}/effective-finance`)
    expect(effective.effective_finance_config.demand_charges.peak_fee.rate).toEqual({ value: '20', unit: 'CNY/kW' })
    expect(effective.effective_finance_config.demand_charges.zero_fee.rate.value).toBe('0')

    await page.goto(`${APP}/projects/${pid}/bindings`)
    // Empty configuration is a first-class saved draft state.
    await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
    await expect(page.getByText(/绑定已保存|Bindings saved/i)).toBeVisible()
    await page.reload()
    await expect(page.getByText(/当前无需量绑定|No demand meter is bound/i)).toBeVisible()

    // Real add/remove and empty-save round trip, before creating billable rows.
    await page.getByRole('button', { name: /添加需量绑定|Add demand binding/i }).click()
    await page.getByRole('textbox', { name: '需量绑定 ID' }).fill('will_remove')
    await page.getByRole('button', { name: /^移除$|^Remove$/i }).last().click()
    await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
    await page.reload()
    await expect(page.getByRole('textbox', { name: '需量绑定 ID' })).toHaveCount(0)

    await page.getByRole('button', { name: /添加需量绑定|Add demand binding/i }).click()
    await page.getByRole('textbox', { name: '需量绑定 ID' }).fill('temporary_meter')
    await page.getByRole('combobox', { name: '需量费项及费率' }).selectOption('peak_fee')
    await page.getByRole('combobox', { name: '计量设备实例' }).last().selectOption('grid1')
    await page.getByRole('combobox', { name: '电力出向计量接口' }).selectOption('electricity_import')
    await page.getByRole('button', { name: /添加需量绑定|Add demand binding/i }).click()
    await page.getByRole('textbox', { name: '需量绑定 ID' }).last().fill('zero_meter')
    await page.getByRole('combobox', { name: '需量费项及费率' }).last().selectOption('zero_fee')
    await page.getByRole('combobox', { name: '计量设备实例' }).last().selectOption('grid1')
    await page.getByRole('combobox', { name: '电力出向计量接口' }).last().selectOption('electricity_import')
    // Edit one binding, then persist both rows in the same draft revision.
    await page.getByRole('textbox', { name: '需量绑定 ID' }).first().fill('main_meter')
    await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
    await expect(page.getByText(/绑定已保存|Bindings saved/i)).toBeVisible()
    await page.reload()
    await expect(page.getByRole('textbox', { name: '需量绑定 ID' }).first()).toHaveValue('main_meter')
    await expect(page.getByRole('textbox', { name: '需量绑定 ID' }).last()).toHaveValue('zero_meter')
    const draft = await browserJson<{ draft: { revision: number; content: { demand_bindings: unknown } } }>(page, 'GET', `/api/projects/${pid}`)
    expect(draft.draft.content.demand_bindings).toEqual({
      main_meter: { charge: 'peak_fee', instance: 'grid1', interface: 'electricity_import' },
      zero_meter: { charge: 'zero_fee', instance: 'grid1', interface: 'electricity_import' },
    })

    // Rebind planning to the new Effective, then freeze actual saved GUI draft.
    await browserJson(page, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: originalPlanning.planning_config, expected_revision: null,
    })
    await browserJson(page, 'POST', `/api/projects/${pid}/versions`, {
      name: 'D3 需量配置', reason: 'snapshot_freeze', expected_revision: draft.draft.revision,
    })
    const precheck = await browserJson<{ report: { diagnostics: Array<{ blocking: boolean }> } }>(page, 'POST', `/api/projects/${pid}/validation/run`, {})
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])

    // Submit and wait through the real task UI (one full Gregorian operating year).
    await page.goto(`${APP}/projects/${pid}/tasks`)
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('0')
    await page.locator('#task-operation-years').fill('1')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    await page.getByRole('button', { name: /提交任务|Submit task/i }).last().click()
    await page.locator('.ies-table__row').first().click()
    await expect(page.getByRole('link', { name: /查看结果|View results/i })).toBeVisible({ timeout: 600_000 })
    await page.getByRole('link', { name: /查看结果|View results/i }).click()

    const listed = await browserJson<{ items: Array<{ id: number }> }>(page, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const taskId: number = listed.items[0].id
    const result = await browserJson<{ result: { financial_evaluation: { monthly_demand: Month[]; annual_operation: Array<{ demand_charge: string }>; currency: string }; evidence: { id: number } } }>(page, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
    const fin = result.result.financial_evaluation
    const months: Month[] = fin.monthly_demand
    expect(months).toHaveLength(24)
    expect(months.filter((r) => r.billing_id === 'main_meter')).toHaveLength(12)
    expect(months.filter((r) => r.billing_id === 'zero_meter')).toHaveLength(12)
    for (const row of months) {
      expect(row.calendar_year).toBe(2025)
      expect(row.financial_year).toBe(1)
      expect(row.peak_unit).toBe('kW')
      expect(Number(row.peak_power)).toBeGreaterThan(0)
      if (row.billing_id === 'main_meter') {
        expect(row.rate).toEqual({ value: '20', unit: 'CNY/kW' })
        expect(Number(row.charge)).toBeCloseTo(20 * Number(row.peak_power), 5)
      } else {
        expect(row.rate).toEqual({ value: '0', unit: 'CNY/kW' })
        expect(Number(row.charge)).toBe(0)
      }
    }
    // Locate the real sealed monthly-bill table by its bound meter identity,
    // rather than assuming an i18n header label (the actual column is “峰值”).
    const demandTable = page.getByRole('table').filter({
      has: page.getByRole('cell', { name: 'main_meter', exact: true }),
    })
    await expect(demandTable.getByRole('row')).toHaveCount(25)
    for (const row of months) {
      const line = demandTable.getByRole('row').filter({ hasText: `${row.calendar_year}-${String(row.calendar_month).padStart(2, '0')}` }).filter({ hasText: row.billing_id })
      await expect(line).toContainText(`${row.peak_power} ${row.peak_unit}`)
      await expect(line).toContainText(`${row.charge} ${fin.currency}`)
    }
    expect(fin.annual_operation[0].demand_charge).toBeTruthy()
    expect(Number(fin.annual_operation[0].demand_charge)).toBeGreaterThan(0)

    // Excel requires an existing immutable system assessment of this exact
    // evidence; perform it in the real Results dialog (no implicit bypass).
    await page.getByRole('button', { name: /重新评估|Reassess/i }).click()
    const assessmentDialog = page.getByRole('dialog')
    await assessmentDialog.getByRole('button', { name: /重新运行系统评估|Rerun system assessment/i }).click()
    await expect(assessmentDialog).toBeHidden()

    const seenExports: number[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && new URL(req.url()).pathname.endsWith('/exports/excel')) {
        const body = req.postDataJSON() as { evidence_package_id: number }
        seenExports.push(body.evidence_package_id)
      }
    })
    const pending = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel|Export Excel/i }).click()
    const download = await pending
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-d3-')), 'report.xlsx')
    await download.saveAs(file)
    const xml = workbookXml(fs.readFileSync(file))
    expect(seenExports.at(-1)).toBe(result.result.evidence.id)
    expect(xml).toContain('main_meter')
    expect(xml).toContain('zero_meter')
    expect(xml).toContain('CNY/kW')
    expect(xml).toContain(String(taskId))
    expect(xml).toContain(fin.annual_operation[0].demand_charge)

    session.assertNoErrors()
    await context.close()
    await api.close()
  })
})
