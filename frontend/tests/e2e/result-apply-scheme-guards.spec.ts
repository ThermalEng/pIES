// C1/C2: 应用方案确认修订接通与写请求生命周期 GUI 真实验收。
//
// 场景复用 S3-B 双 heat_pump 脚手架(真实 HiGHS 求解), 但提交两个优化任务使
// 证据选择器渲染; 场景准备走公开 API, 并发修订/确认/取消走 GUI 或同源 Cookie
// 会话 page.request(不新登录、不互踢); 不注库、不伪造 ComputeResult。
// 断言:
// A(C1): 打开确认框显示确认时草稿修订; 对话框打开后经公开接口新增草稿修订,
//   确认应用得到可见 409 冲突, 不创建版本、不替换新编辑(hp3 保留)。
// B(C2): 延迟 apply-result 真实服务端响应(先取回再持有回传, 证明已提交语义),
//   飞行中确认/取消/证据选择禁用、Esc 与强制点确认无法产生第二 POST、busy 不
//   提前解除; 放行后成功横幅与摘要刷新, 只发一次请求。
// C(C2): 应用 POST 本身 500 时确认框错误可见、原工作状态不变、busy 解除。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

const EVALUATION = {
  evaluation_years: 1,
  construction_period: 0,
  operation_years: 1,
  discount_rate: 0.08,
  construction_payments: [{ t: 0, ratio: 1 }],
  maintenance_expenses: [],
}

test.describe('C1/C2: 应用方案修订与请求生命周期', () => {
  test.setTimeout(1_200_000)

  test('确认时修订冲突可见, 飞行中锁定, 失败不污染', async ({ browser }) => {
    const username = uniqueName('es-guard')
    const password = strongPassword('Init')
    const projectName = uniqueName('guard')
    const profileId = `guard-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)

    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'guard-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'guard-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'guard-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed',
              finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
            hp2: {
              type: 'costed',
              finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    }, token)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        objective: {
          sense: 'minimize',
          expression: 'hp1__upfront_capex + hp2__upfront_capex + 1 year * hp1__annual_fixed_om + 1 year * hp2__annual_fixed_om',
        },
        variables: {
          hp1_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW' },
          hp2_cap: { device_ref: 'hp2', parameter: 'rated_heat_kw', lower_bound: '200', upper_bound: '1000', unit: 'kW' },
        },
        constraints: {},
      },
      expected_revision: 1,
    }, token)
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: '规划双泵', reason: 'manual_save', expected_revision: put2.revision,
    }, token)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])

    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, token)
    const taskIds: number[] = []
    for (const key of ['guard-a', 'guard-b']) {
      const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
        task_type: 'optimization',
        config: { config_revision: cfg.revision },
        evaluation: EVALUATION,
        idempotency_key: `${key}-${Date.now()}`,
        parent_task_id: null,
      }, token)
      taskIds.push(task.task.id)
    }
    for (const taskId of taskIds) {
      await expect(async () => {
        const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, token)
        expect(detail.task.status).toBe('completed')
      }).toPass({ timeout: 420_000, intervals: [10_000] })
    }
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    let applyPosts = 0
    await page.route('**/apply-result', async (route) => {
      if (route.request().method() === 'POST') applyPosts += 1
      await route.continue()
    })
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    // 两个证据包 → 证据选择器渲染(飞行中禁用断言的目标)。
    await expect(page.getByLabel('证据包状态')).toBeVisible({ timeout: 10_000 })

    // ---- A(C1): 确认时修订捕获, 打开后并发修订 → 可见 409, 无版本、无覆盖。
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    const revText = await dialog.getByText(/确认基线：当前草稿修订/).textContent()
    const revMatch = revText?.match(/r(\d+)/)
    expect(revMatch).not.toBeNull()
    const revAtConfirm = Number(revMatch?.[1])
    const viewA = await requestJson(page.request, 'GET', `/api/projects/${pid}`)
    expect(viewA.draft.revision).toBe(revAtConfirm)
    // 对话框打开后经公开接口新增草稿修订(hp3)。
    const bump = await requestJson(page.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: revAtConfirm,
      commands: [{
        id: 'guard-dev-hp3', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp3', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 600, cop: 3.5 }, position: { x: 9.0, y: 9.0 },
        },
      }],
    })
    expect(bump.revision).toBe(revAtConfirm + 1)
    await dialog.getByRole('button', { name: /确定|确认/ }).click()
    // 409 修订冲突经 message_key 译为可见文案(后端原语由后端定向测试锁定)。
    await expect(dialog.getByText(/保存冲突/)).toBeVisible({ timeout: 30_000 })
    await expect(dialog).toBeVisible()
    expect(applyPosts).toBe(1)
    // 不创建版本、不替换新编辑。
    const versionsA = await requestJson(page.request, 'GET', `/api/projects/${pid}/versions`)
    expect(versionsA.versions.map((v: { version_no: number }) => v.version_no).sort()).toEqual([1, 2])
    const viewA2 = await requestJson(page.request, 'GET', `/api/projects/${pid}`)
    expect(viewA2.draft.revision).toBe(revAtConfirm + 1)
    const namesA = (viewA2.draft.content.model.devices as Array<{ name: string }>).map((d) => d.name)
    expect(namesA).toContain('hp3')
    await dialog.getByRole('button', { name: /取消/ }).click()
    await expect(dialog).toHaveCount(0)
    expect(applyPosts).toBe(1)

    // ---- B(C2): 真实响应持有回传, 飞行中锁定, 放行后成功刷新。
    // A 阶段的并发修订发生在 workbench 状态加载之后: 重载使确认基线取到最新。
    await page.reload()
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    await page.unrouteAll({ behavior: 'wait' })
    let releaseHeld!: () => void
    const holdGate = new Promise<void>((resolve) => {
      releaseHeld = resolve
    })
    await page.route('**/apply-result', async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue()
        return
      }
      // 先取得真实服务端响应(此时服务端已提交), 再持有回传。
      const response = await route.fetch()
      applyPosts += 1
      await holdGate
      await route.fulfill({ response })
    })
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 })
    const revTextB = await dialog.getByText(/确认基线：当前草稿修订/).textContent()
    expect(revTextB).toContain(`r${revAtConfirm + 1}`)
    await dialog.getByRole('button', { name: /确定|确认/ }).click()
    // 服务端已响应(已提交)但回传被持有: 此时仍 busy。
    await expect.poll(async () => applyPosts, { timeout: 60_000 }).toBe(2)
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('button', { name: /确定|确认/ })).toBeDisabled()
    await expect(dialog.getByRole('button', { name: /取消/ })).toBeDisabled()
    await expect(page.getByLabel('证据包状态')).toBeDisabled()
    // 真实关闭尝试: Esc 不关闭对话框。
    await page.keyboard.press('Escape')
    await expect(dialog).toBeVisible()
    // 真实重复确认尝试: 强制点击不产生第二 POST。
    await dialog.getByRole('button', { name: /确定|确认/ }).click({ force: true })
    expect(applyPosts).toBe(2)
    releaseHeld()
    await expect(page.getByText(/已应用方案/)).toBeVisible({ timeout: 60_000 })
    expect(applyPosts).toBe(2)
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByLabel('证据包状态')).toBeEnabled()
    const versionsB = await requestJson(page.request, 'GET', `/api/projects/${pid}/versions`)
    expect(versionsB.versions.map((v: { version_no: number }) => v.version_no).sort()).toEqual([1, 2, 3])

    // ---- C(C2): 应用 POST 本身失败 → 错误可见、状态不变、busy 解除。
    await page.unrouteAll({ behavior: 'wait' })
    await page.route('**/apply-result', async (route) => {
      if (route.request().method() === 'POST') {
        applyPosts += 1
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"guard-boom"}' })
        return
      }
      await route.continue()
    })
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: /确定|确认/ }).click()
    await expect(dialog.locator('.ies-alert--error')).toBeVisible({ timeout: 30_000 })
    await expect(dialog).toBeVisible()
    expect(applyPosts).toBe(3)
    // busy 已解除: 确认/取消恢复可用, 对话框可正常关闭。
    await expect(dialog.getByRole('button', { name: /确定|确认/ })).toBeEnabled()
    await expect(dialog.getByRole('button', { name: /取消/ })).toBeEnabled()
    const versionsC = await requestJson(page.request, 'GET', `/api/projects/${pid}/versions`)
    expect(versionsC.versions.map((v: { version_no: number }) => v.version_no).sort()).toEqual([1, 2, 3])
    await dialog.getByRole('button', { name: /取消/ }).click()
    await expect(dialog).toHaveCount(0)

    session.assertNoErrors([
      { method: 'POST', path: `/api/projects/${pid}/apply-result`, status: 409, times: 1 },
      { method: 'POST', path: `/api/projects/${pid}/apply-result`, status: 500, times: 1 },
    ])
    await context.close()
  })
})
