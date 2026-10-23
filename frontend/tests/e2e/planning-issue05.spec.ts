// ISSUE-05: public PlanningConfig UI; only account/project/finance prerequisites
// are seeded through API. Planning read, edit, save, reload, and conflict use real GUI.
import { test, expect } from '@playwright/test'
import { createSession, attachTab, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { financeProfile } from './financial-scaffold'

test('planning editor: new revision, readback, conflict keeps edits and reload is explicit', async ({ browser }) => {
  const username = uniqueName('planning-ui')
  const password = strongPassword('Init')
  const projectName = uniqueName('Planning GUI acceptance')
  const profileId = uniqueName('plan-profile')
  const apiContext = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
  await createEngineer(apiContext.request, username, password)
  const token = await loginToken(apiContext.request, username, password)
  const created = await requestJson(apiContext.request, 'POST', '/api/projects', {
    name: projectName, baseline_resolution: '1h', baseline_start_year: 2026, baseline_scenario_mode: 'single',
  }, token)
  const projectId = created.project.id as number
  await requestJson(apiContext.request, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(apiContext.request, 'PUT', '/api/projects/' + projectId + '/finance-profile', {
    profile_ref: { id: profileId },
  }, token)
  await apiContext.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  // attachTab replaces the harness callback; retain the main tab monitor separately.
  const assertMainErrors = session.assertNoErrors
  await page.goto('/projects/' + projectId + '/planning')
  await expect(page.getByText(/尚未保存规划配置|No saved planning configuration/)).toBeVisible()
  await page.locator('#planning-sense').selectOption('maximize')
  await page.locator('#planning-expression').fill('system.after_tax_npv')
  await page.getByRole('button', { name: /添加规划变量|Add variable/ }).click()
  await page.locator('[id^="planning-var-name-"]').fill('capacity')
  await page.locator('[id^="planning-var-device-"]').fill('hp1')
  await page.locator('[id^="planning-var-param-"]').fill('rated_heat_kw')
  await page.locator('[id^="planning-var-lower-"]').fill('50')
  await page.locator('[id^="planning-var-upper-"]').fill('1000')
  await page.locator('[id^="planning-var-unit-"]').fill('kW')
  await page.getByRole('button', { name: /添加约束|Add constraint/ }).click()
  await page.locator('[id^="planning-con-name-"]').fill('max_carbon')
  await page.locator('[id^="planning-con-type-"]').selectOption('co2_limit')
  await page.locator('[id^="planning-con-expression-"]').fill('1 <= 2')
  // Hold the real PUT at the browser transport boundary. No response mocking:
  // edits (including adding/removing rows) must be blocked until it completes.
  let releaseSave!: () => void
  let saveIntercepted!: () => void
  const saveGate = new Promise<void>((resolve) => { releaseSave = resolve })
  const saveStarted = new Promise<void>((resolve) => { saveIntercepted = resolve })
  const planningPath = '/api/projects/' + projectId + '/planning-config'
  const planningRoute = '**' + planningPath
  await page.route(planningRoute, async (route) => {
    if (route.request().method() === 'PUT') {
      saveIntercepted()
      await saveGate
    }
    await route.continue()
  })
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await saveStarted
  await expect(page.locator('#planning-expression')).toBeDisabled()
  await expect(page.locator('[id^="planning-var-lower-"]')).toBeDisabled()
  await expect(page.locator('[id^="planning-con-expression-"]')).toBeDisabled()
  await expect(page.getByRole('button', { name: /添加规划变量|Add variable/ })).toBeDisabled()
  await expect(page.getByRole('button', { name: /添加约束|Add constraint/ })).toBeDisabled()
  releaseSave()
  await expect(page.getByText(/规划配置已保存 revision v1|Planning config saved, revision v1/)).toBeVisible()
  await page.unroute(planningRoute)
  await expect(page.locator('#planning-expression')).toBeEnabled()

  const response = await page.request.get('/api/projects/' + projectId + '/planning-config')
  const body = await response.json()
  expect(body.planning_config.objective).toEqual({ sense: 'maximize', expression: 'system.after_tax_npv' })
  expect(body.planning_config.variables.capacity).toEqual({
    device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW',
  })
  expect(body.planning_config.constraints.max_carbon).toEqual({ type: 'co2_limit', expression: '1 <= 2', enabled: true })

  await page.reload()
  await expect(page.locator('#planning-expression')).toHaveValue('system.after_tax_npv')
  const second = await context.newPage()
  attachTab(session, second)
  const assertSecondErrors = session.assertNoErrors
  await second.goto('/projects/' + projectId + '/planning')
  await expect(second.locator('#planning-expression')).toHaveValue('system.after_tax_npv')
  await second.locator('#planning-expression').fill('system.after_tax_npv')
  await second.locator('[id^="planning-var-upper-"]').fill('900')
  await second.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(second.getByText(/revision v2/).first()).toBeVisible()
  await second.close()

  await page.locator('[id^="planning-var-lower-"]').fill('60')
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.getByText(/当前编辑已保留|Your edits are preserved/)).toBeVisible()
  await expect(page.locator('[id^="planning-var-lower-"]')).toHaveValue('60')
  // Delay the real readback to verify reload also locks the form until response.
  let releaseRead!: () => void
  let readIntercepted!: () => void
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve })
  const readStarted = new Promise<void>((resolve) => { readIntercepted = resolve })
  await page.route(planningRoute, async (route) => {
    if (route.request().method() === 'GET') {
      readIntercepted()
      await readGate
    }
    await route.continue()
  })
  await page.getByRole('button', { name: /重新加载最新规划|Reload latest planning/ }).click()
  await readStarted
  await expect(page.locator('#planning-expression')).toBeDisabled()
  await expect(page.locator('[id^="planning-var-lower-"]')).toBeDisabled()
  await expect(page.getByRole('button', { name: /添加规划变量|Add variable/ })).toBeDisabled()
  releaseRead()
  await expect(page.locator('[id^="planning-var-upper-"]')).toHaveValue('900')
  await expect(page.locator('#planning-expression')).toBeEnabled()
  await page.unroute(planningRoute)
  await expect(page.locator('[id^="planning-var-lower-"]')).toHaveValue('50')
  // The initial missing revision and intentional optimistic-lock conflict are
  // the only expected browser HTTP errors. The second tab has none.
  assertMainErrors([
    { method: 'GET', path: planningPath, status: 404, times: 1 },
    { method: 'PUT', path: planningPath, status: 409, times: 1 },
  ])
  assertSecondErrors()
  await context.close()
})