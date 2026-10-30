// S3 user chain: only reusable project/model/finance prerequisites are API seeded.
// Planning document edit/save, form edit/save, version freeze and task submission
// are performed through the real browser; backend/HiGHS/Worker are not mocked.
import { test, expect } from '@playwright/test'
import { createSession, attachTab, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

test('S3 YAML -> form -> saved revision -> real same-solve constrained result', async ({ browser }) => {
  test.setTimeout(900_000)
  const username = uniqueName('step-s3')
  const password = strongPassword('Init')
  const api = await browser.newContext({ baseURL: APP })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const pid = await scaffoldProject(
    requestJson, api.request, token, uniqueName('step-S3'), uniqueName('s3-profile'),
  )
  await api.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  const planningPath = `/api/projects/${pid}/planning-config`
  const failures: Array<{ method: string; path: string; status: number }> = []
  page.on('response', (res) => {
    if (new URL(res.url()).pathname === planningPath && res.status() >= 400) {
      failures.push({ method: res.request().method(), path: planningPath, status: res.status() })
    }
  })
  await page.goto(`/projects/${pid}/planning`)
  await expect(page.locator('#planning-expression')).toBeVisible()

  // 1. Partial form -> YAML: switching to document view does not block on incomplete form.
  await page.locator('#planning-expression').fill('hp_partial')
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  await expect(page.locator('#planning-yaml')).toBeVisible()
  expect(await page.locator('#planning-yaml').inputValue()).toContain('hp_partial')
  await expect(page.getByRole('alert')).toHaveCount(0)

  // 2. Legal omitted constraints and explicit enabled: false round-trips correctly.
  const omittedConstraintsYaml = `objective:
  sense: minimize
  expression: hp_partial
variables:
  v1:
    device_ref: hp1
    parameter: rated_heat_kw
`
  await page.locator('#planning-yaml').fill(omittedConstraintsYaml)
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.locator('#planning-expression')).toHaveValue('hp_partial')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.locator('[id^="planning-con-name-"]')).toHaveCount(0)

  // Constraint with omitted enabled defaults to enabled (true) in form; explicit enabled: false is false.
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  const enabledSemanticsYaml = `objective:
  sense: minimize
  expression: hp_partial
variables:
  v1:
    device_ref: hp1
    parameter: rated_heat_kw
constraints:
  c_omitted:
    type: ratio
    expression: 1 <= 2
  c_disabled:
    type: ratio
    expression: 1 <= 3
    enabled: false
`
  await page.locator('#planning-yaml').fill(enabledSemanticsYaml)
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  const omittedSelect = page.locator('[id^="planning-con-enabled-"]').first()
  const disabledSelect = page.locator('[id^="planning-con-enabled-"]').nth(1)
  expect(await omittedSelect.inputValue()).toBe('true')
  expect(await disabledSelect.inputValue()).toBe('false')

  // Switch back to YAML for the rest of the test suite.
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()

  const yaml = `objective:
  sense: minimize
  expression: grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om
variables:
  hp_cap:
    device_ref: hp1
    parameter: rated_heat_kw
    lower_bound: 50.000000000000000000000000000001
    upper_bound: '1000'
    unit: kW
constraints:
  scalar:
    type: ratio
    expression: 1 <= 2
    enabled: true
  heat_gate:
    type: step_relation
    references:
      heat:
        instance: hp1
        interface: heat_out
    expression: heat[t] <= 0.5 * hp_cap
    enabled: true
  disabled_gate:
    type: step_relation
    references:
      heat:
        instance: hp1
        interface: heat_out
    expression: heat[t] <= 0 kW
    enabled: false
`
  // An unnamed new row must not overwrite an existing variable_2 or constraint_2.
  await page.locator('#planning-yaml').fill(yaml.replaceAll('hp_cap', 'variable_2'))
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await page.getByRole('button', { name: /添加规划变量|Add variable/ }).click()
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  expect(await page.locator('#planning-yaml').inputValue()).toContain('heat[t] <= 0.5 * variable_2')
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.locator('[id^="planning-var-name-"]')).toHaveCount(2)
  await expect(page.locator('[id^="planning-var-name-"]').first()).toHaveValue('variable_2')
  await expect(page.locator('[id^="planning-var-name-"]').nth(1)).toHaveValue('')
  await expect(page.locator('[id^="planning-var-param-"]').first()).toHaveValue('rated_heat_kw')
  await expect(page.locator('[id^="planning-var-lower-"]').first()).toHaveValue('50.000000000000000000000000000001')
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  const singleConstraint = yaml.slice(0, yaml.indexOf('\nconstraints:')) + `
constraints:
  constraint_2:
    type: step_relation
    references:
      heat:
        instance: hp1
        interface: heat_out
    expression: heat[t] <= 0.5 * hp_cap
    enabled: false
`
  await page.locator('#planning-yaml').fill(singleConstraint)
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await page.getByRole('button', { name: /添加约束|Add constraint/ }).click()
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  expect(await page.locator('#planning-yaml').inputValue()).toContain('interface: heat_out')
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.locator('[id^="planning-con-name-"]')).toHaveCount(2)
  await expect(page.locator('[id^="planning-con-name-"]').first()).toHaveValue('constraint_2')
  await expect(page.locator('[id^="planning-con-name-"]').nth(1)).toHaveValue('')
  await expect(page.locator('[id^="planning-con-expression-"]').first()).toHaveValue('heat[t] <= 0.5 * hp_cap')
  await expect(page.locator('[id^="planning-con-enabled-"]').first()).toHaveValue('false')
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  await page.locator('#planning-yaml').fill(yaml)

  // Hold real network PUT; UI should lock document and view selector, then recover.
  let release!: () => void
  let started!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const startedPromise = new Promise<void>(resolve => { started = resolve })
  await page.route('**' + planningPath, async (route) => {
    if (route.request().method() === 'PUT') {
      started()
      await gate
    }
    await route.continue()
  })
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await startedPromise
  await expect(page.locator('#planning-yaml')).toBeDisabled()
  await expect(page.getByRole('button', { name: /表单编辑|Form editor/ })).toBeDisabled()
  release()
  await expect(page.getByText(/revision v2/).first()).toBeVisible()
  await page.unroute('**' + planningPath)
  await expect(page.locator('#planning-yaml')).toBeEnabled()

  await page.reload()
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  await expect(page.locator('#planning-yaml')).toContainText('heat[t] <= 0.5 * hp_cap')
  await page.getByRole('button', { name: /表单编辑|Form editor/ }).click()
  await expect(page.locator('[id^="planning-con-type-"]')).toHaveCount(3)
  await page.locator('[id^="planning-var-upper-"]').fill('900')
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.getByText(/revision v3/).first()).toBeVisible()
  await page.getByRole('button', { name: /完整 YAML 文档|Full YAML document/ }).click()
  const persisted = await page.request.get(planningPath)
  expect(persisted.ok()).toBeTruthy()
  const persistedBody = (await persisted.json()).planning_config
  expect(persistedBody.variables.hp_cap.lower_bound).toBe('50.000000000000000000000000000001')
  expect(persistedBody.variables.hp_cap.upper_bound).toBe('900')
  expect(persistedBody.constraints.scalar.type).toBe('ratio')
  expect(persistedBody.constraints.heat_gate.references.heat).toEqual({ instance: 'hp1', interface: 'heat_out' })
  expect(persistedBody.constraints.disabled_gate.enabled).toBe(false)

  const stable = await page.locator('#planning-yaml').inputValue()
  // Syntax failure: local editor keeps the user's exact erroneous bytes.
  const malformed = stable + '\nbad_yaml: ['
  await page.locator('#planning-yaml').fill(malformed)
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.locator('#planning-yaml')).toHaveValue(malformed)
  await expect(page.getByRole('alert').first()).toBeVisible()

  // Semantic failures: dispatch the real PUT, display the server's public
  // diagnostic rather than discarding or converting the input.
  for (const invalid of [
    stable.replace('interface: heat_out', 'interface: absent_s3_port'),
    stable.replace('heat[t] <= 0.5 * hp_cap', 'heat[t] <= 1 kWh'),
  ]) {
    await page.locator('#planning-yaml').fill(invalid)
    const pending = page.waitForResponse(res =>
      new URL(res.url()).pathname === planningPath && res.request().method() === 'PUT')
    await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
    const response = await pending
    expect(response.ok()).toBe(false)
    await expect(page.locator('#planning-yaml')).toHaveValue(invalid)
    await expect(page.getByRole('alert').first()).toBeVisible()
  }

  await page.locator('#planning-yaml').fill(stable)
  const assertMainErrors = session.assertNoErrors
  // Another tab advances revision: conflict preserves the first tab's draft,
  // with explicit reload (also locked during its real GET).
  const other = await context.newPage()
  attachTab(session, other)
  const assertOtherErrors = session.assertNoErrors
  await other.goto(`/projects/${pid}/planning`)
  await expect(other.locator('#planning-expression')).toBeVisible()
  await other.locator('[id^="planning-var-upper-"]').fill('850')
  await other.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(other.getByText(/revision v4/).first()).toBeVisible()
  assertOtherErrors()
  await other.close()
  await page.getByRole('button', { name: /保存规划配置|Save planning config/ }).click()
  await expect(page.getByText(/当前编辑已保留|Your edits are preserved/)).toBeVisible()
  await expect(page.locator('#planning-yaml')).toHaveValue(stable)
  let releaseRead!: () => void
  let readStart!: () => void
  const readGate = new Promise<void>(resolve => { releaseRead = resolve })
  const readStarted = new Promise<void>(resolve => { readStart = resolve })
  await page.route('**' + planningPath, async route => {
    if (route.request().method() === 'GET') { readStart(); await readGate }
    await route.continue()
  })
  await page.getByRole('button', { name: /重新加载最新规划|Reload latest planning/ }).click()
  await readStarted
  await expect(page.locator('#planning-yaml')).toBeDisabled()
  releaseRead()
  await expect(page.locator('#planning-yaml')).toBeEnabled()
  await page.unroute('**' + planningPath)
  expect(await page.locator('#planning-yaml').inputValue()).toContain('850')

  // Version is frozen *after* the UI-edited plan; task is submitted through
  // the task-center page and solved by real HiGHS, not an API fixture shortcut.
  await page.goto(`/projects/${pid}/versions`)
  await page.getByRole('button', { name: /创建版本|Create version/ }).first().click()
  await page.locator('#version-name').fill('S3 step gate')
  await page.getByRole('button', { name: /^创建$|^Create$/ }).click()
  await expect(page.getByText('S3 step gate')).toBeVisible()
  await page.goto(`/projects/${pid}/tasks`)
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('0')
  await page.locator('#task-operation-years').fill('1')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  await page.getByRole('button', { name: /提交任务|Submit task/i }).last().click()
  await page.locator('.ies-table__row').first().click()
  const badge = page.locator('.ies-task-detail .ies-badge__label').first()
  await expect(async () => expect(await badge.textContent()).toMatch(/已完成|Completed/))
    .toPass({ timeout: 420_000, intervals: [10_000] })
  await page.getByRole('link', { name: /查看结果|View results/i }).click()
  const row = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp_cap' })
  await expect(row).toBeVisible()
  // heat[t] = 30 kW and heat[t] <= 0.5 * hp_cap => optimum hp_cap = 60 kW,
  // whereas the original unconstrained optimum is 50 kW.
  await expect(row.getByText(/60\.0000/)).toBeVisible()
  assertMainErrors(failures)
  await context.close()
})
