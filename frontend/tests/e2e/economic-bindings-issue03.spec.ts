// ISSUE-03 targeted GUI acceptance (Docker Playwright, deferred per review plan).
// Setup API creates only user/project/model/registered finance, not binding sections.
// All cost/tariff binding edits, saving, reopens and conflicts use the real UI.
import { test, expect } from '@playwright/test'
import { createSession, attachTab, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { financeProfile } from './financial-scaffold'

test('cost and tariff GUI: no inferred cost, atomic draft save, reload, 409 preserves edits', async ({ browser }) => {
  const username = uniqueName('issue03')
  const password = strongPassword('Init')
  const projectName = uniqueName('Issue03 bindings')
  const profileId = uniqueName('issue03-profile')
  const api = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const created = await requestJson(api.request, 'POST', '/api/projects', {
    name: projectName, baseline_resolution: '1h', baseline_start_year: 2026, baseline_scenario_mode: 'single',
  }, token)
  const pid = created.project.id as number
  await requestJson(api.request, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(api.request, 'PUT', '/api/projects/' + pid + '/finance-profile', {
    profile_ref: { id: profileId },
  }, token)
  const before = await requestJson(api.request, 'GET', '/api/projects/' + pid, undefined, token)
  const modelCommands = [
    { id: 'issue03-grid', unit: 'model', type: 'model.upsert_device', payload: {
      name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
      params: { max_import_power_kw: 800, max_export_power_kw: 100 },
    } },
    { id: 'issue03-hp', unit: 'model', type: 'model.upsert_device', payload: {
      name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new', params: { rated_heat_kw: 500, cop: 3.5 },
    } },
  ]
  await requestJson(api.request, 'PUT', '/api/projects/' + pid + '/draft', {
    expected_revision: before.draft.revision, commands: modelCommands,
  }, token)
  await api.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  const assertMainErrors = session.assertNoErrors
  await page.goto('/projects/' + pid + '/bindings')
  await expect(page.locator('#binding-mode-grid1')).toHaveValue('')
  await expect(page.locator('#binding-mode-hp1')).toHaveValue('')
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveCount(0)

  // No implicit type:none, costed finance_type, or metering from the device model.
  await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
  await expect(page.getByText(/尚未明确成本参与方式|Choose cost participation/)).toBeVisible()
  await page.locator('#binding-mode-grid1').selectOption('none')
  await page.locator('#binding-mode-hp1').selectOption('costed')
  await expect(page.locator('#binding-finance-hp1')).toHaveValue('')
  await page.locator('#binding-finance-hp1').selectOption('heat_pump_system')

  await page.getByRole('button', { name: /添加 driver 关联|Add driver mapping/i }).click()
  await page.locator('input[id^="binding-driver-"]').nth(0).fill('heat_kw')
  await page.locator('select[id^="binding-source-"]').nth(0).selectOption('property')
  await page.locator('input[id^="binding-target-"]').nth(0).fill('rated_heat_kw')
  await page.getByRole('button', { name: /添加 driver 关联|Add driver mapping/i }).click()
  await page.locator('input[id^="binding-driver-"]').nth(1).fill('consumed_kwh')
  await page.locator('select[id^="binding-source-"]').nth(1).selectOption('interface')
  await page.locator('input[id^="binding-target-"]').nth(1).fill('electricity_in')
  await page.locator('select[id^="binding-agg-"]').selectOption('integrate_positive')

  await page.getByRole('button', { name: /添加计费绑定|Add tariff binding/i }).click()
  await page.locator('input[id^="tariff-id-"]').fill('import_meter')
  await page.locator('select[id^="tariff-price-"]').selectOption('grid_import')
  await page.locator('select[id^="tariff-instance-"]').selectOption('grid1')
  await page.locator('input[id^="tariff-interface-"]').fill('electricity_import')
  await page.locator('select[id^="tariff-agg-"]').selectOption('integrate_positive')

  // Delay actual PUT without mocking its response; all fields are locked in flight.
  let releaseSave!: () => void
  let signalSave!: () => void
  const saveGate = new Promise<void>((resolve) => { releaseSave = resolve })
  const saveStarted = new Promise<void>((resolve) => { signalSave = resolve })
  const draftPath = '/api/projects/' + pid + '/draft'
  await page.route('**' + draftPath, async (route) => {
    if (route.request().method() === 'PUT') {
      signalSave()
      await saveGate
    }
    await route.continue()
  })
  await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
  await saveStarted
  await expect(page.locator('#binding-mode-grid1')).toBeDisabled()
  await expect(page.locator('input[id^="tariff-interface-"]')).toBeDisabled()
  await expect(page.getByRole('button', { name: /添加计费绑定|Add tariff binding/i })).toBeDisabled()
  releaseSave()
  await expect(page.getByText(/绑定已保存|Bindings saved/)).toBeVisible()
  await page.unroute('**' + draftPath)

  const first = await page.request.get('/api/projects/' + pid)
  const firstContent = (await first.json()).draft.content
  expect(firstContent.finance_binding.instances).toEqual({
    grid1: { type: 'none' },
    hp1: { type: 'costed', finance_type: 'heat_pump_system', drivers: {
      heat_kw: { property: 'rated_heat_kw' },
      consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
    } },
  })
  expect(firstContent.tariff_bindings).toEqual({
    import_meter: { price: 'grid_import', instance: 'grid1', interface: 'electricity_import', aggregation: 'integrate_positive' },
  })
  await page.reload()
  await expect(page.locator('#binding-mode-hp1')).toHaveValue('costed')
  await expect(page.locator('input[id^="binding-driver-"]')).toHaveCount(2)
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveValue('import_meter')

  const second = await context.newPage()
  attachTab(session, second)
  const assertSecondErrors = session.assertNoErrors
  await second.goto('/projects/' + pid + '/bindings')
  await expect(second.locator('#binding-mode-grid1')).toHaveValue('none')
  await second.getByRole('button', { name: /添加计费绑定|Add tariff binding/i }).click()
  await second.locator('input[id^="tariff-id-"]').last().fill('backup_meter')
  await second.locator('select[id^="tariff-price-"]').last().selectOption('grid_import')
  await second.locator('select[id^="tariff-instance-"]').last().selectOption('grid1')
  await second.locator('input[id^="tariff-interface-"]').last().fill('electricity_import')
  await second.locator('select[id^="tariff-agg-"]').last().selectOption('integrate_positive')
  await second.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
  await expect(second.getByText(/绑定已保存|Bindings saved/)).toBeVisible()
  await second.close()

  await page.locator('input[id^="tariff-id-"]').fill('edited_meter')
  await page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i }).click()
  await expect(page.getByText(/草稿修订冲突|draft revision|修改|conflict/i).first()).toBeVisible()
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveValue('edited_meter')
  await page.getByRole('button', { name: /重载最新草稿|Reload latest draft/i }).click()
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveCount(2)
  await expect.poll(() => page.locator('input[id^="tariff-id-"]').evaluateAll(
    inputs => inputs.map(input => (input as HTMLInputElement).value).sort(),
  )).toEqual(['backup_meter', 'import_meter'])
  assertMainErrors([{ method: 'PUT', path: draftPath, status: 409, times: 1 }])
  assertSecondErrors()
  await context.close()
})


test('empty model: explicitly remove orphan cost and tariff bindings, then persist empty sections', async ({ browser }) => {
  const username = uniqueName('issue03-empty')
  const password = strongPassword('Init')
  const profileId = uniqueName('issue03-empty-profile')
  const api = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const created = await requestJson(api.request, 'POST', '/api/projects', {
    name: uniqueName('Issue03 stale bindings'),
    baseline_resolution: '1h',
    baseline_start_year: 2026,
    baseline_scenario_mode: 'single',
  }, token)
  const pid = created.project.id as number
  await requestJson(api.request, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(api.request, 'PUT', '/api/projects/' + pid + '/finance-profile', {
    profile_ref: { id: profileId },
  }, token)
  const before = await requestJson(api.request, 'GET', '/api/projects/' + pid, undefined, token)
  // Fixture only: the last device was deleted, leaving its old finance and meter
  // sections behind. The cleanup itself must take place in the tested GUI.
  await requestJson(api.request, 'PUT', '/api/projects/' + pid + '/draft', {
    expected_revision: before.draft.revision,
    commands: [
      { id: 'issue03-empty-add', unit: 'model', type: 'model.upsert_device', payload: {
        name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
        params: { max_import_power_kw: 100, max_export_power_kw: 0 },
      } },
      { id: 'issue03-empty-cost', unit: 'assembly', type: 'assembly.set_finance_binding', payload: {
        instances: { grid1: { type: 'none' } },
      } },
      { id: 'issue03-empty-meter', unit: 'assembly', type: 'assembly.set_tariff_bindings', payload: {
        bindings: {
          import_meter: {
            price: 'grid_import', instance: 'grid1',
            interface: 'electricity_import', aggregation: 'integrate_positive',
          },
        },
      } },
      { id: 'issue03-empty-remove', unit: 'model', type: 'model.remove_device', payload: { name: 'grid1' } },
    ],
  }, token)
  await api.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.goto('/projects/' + pid + '/bindings')
  await expect(page.locator('#binding-mode-grid1')).toHaveCount(0)
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveValue('import_meter')
  const saveButton = page.getByRole('button', { name: /保存成本、能源与需量绑定|Save cost, energy & demand bindings/i })
  // A model with zero devices can still persist an explicit cleanup.
  await expect(saveButton).toBeEnabled()
  await saveButton.click()
  await expect(page.getByText(/存在旧实例关联|Remove or reconcile bindings/)).toBeVisible()

  // Explicitly discard each obsolete association, not an implicit side effect
  // of the model deletion or an automatic selection of type:none.
  const removeButtons = page.getByRole('button', { name: /^移除$|^Remove$/ })
  await expect(removeButtons).toHaveCount(2)
  await removeButtons.first().click() // orphan finance_binding.instances.grid1
  await expect(removeButtons).toHaveCount(1)
  await removeButtons.click() // stale tariff_bindings.import_meter
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveCount(0)
  await expect(saveButton).toBeEnabled()
  await saveButton.click()
  await expect(page.getByText(/绑定已保存|Bindings saved/)).toBeVisible()

  const saved = await page.request.get('/api/projects/' + pid)
  expect(saved.ok()).toBe(true)
  const content = (await saved.json()).draft.content
  expect(content.model.devices).toEqual([])
  expect(content.finance_binding).toEqual({ instances: {} })
  expect(content.tariff_bindings).toEqual({})

  await page.reload()
  await expect(saveButton).toBeEnabled()
  await expect(page.locator('#binding-mode-grid1')).toHaveCount(0)
  await expect(page.locator('input[id^="tariff-id-"]')).toHaveCount(0)
  await expect(page.getByText(/原草稿存在已移除的设备绑定|Bindings for removed instances/)).toHaveCount(0)
  session.assertNoErrors()
  await context.close()
})
