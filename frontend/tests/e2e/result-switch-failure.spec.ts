// C2 结果失败可见 + C3 切换状态归属定向验收。
//
// 场景: 同一项目两条真实 HiGHS 优化求解(A: hp_cap 下界 50 → 50kW,
// B: 下界 60 → 60kW, 版本 v2/v3)。场景准备走公开 API(脚手架/配置/
// 版本/任务提交与轮询), 切换查看、失败呈现走 GUI; 不注库、不伪造
// ComputeResult, 画布拖放不测。
// C2: B 的结果 GET 失败必须显示错误条, 不得渲染“暂无候选/没有解”空态。
// C3: 切换证据包即清理旧预览/来源/选择/确认; 迟到响应不覆盖新选中;
// 加载中无旧值、无旧来源、无确认框。

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

async function submitOptimization(
  request: Parameters<typeof requestJson>[0],
  pid: number,
  token: string,
  key: string,
): Promise<number> {
  const cfg = await requestJson(request, 'GET', `/api/projects/${pid}/config`, undefined, token)
  const task = await requestJson(request, 'POST', `/api/projects/${pid}/tasks`, {
    task_type: 'optimization',
    config: { config_revision: cfg.revision },
    evaluation: EVALUATION,
    idempotency_key: key,
    parent_task_id: null,
  }, token)
  return task.task.id as number
}

async function waitCompleted(
  request: Parameters<typeof requestJson>[0],
  pid: number,
  taskId: number,
  token: string,
): Promise<void> {
  await expect(async () => {
    const detail = await requestJson(request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, token)
    expect(detail.task.status).toBe('completed')
  }).toPass({ timeout: 420_000, intervals: [10_000] })
}

test.describe('C2/C3: 结果失败可见与切换状态归属', () => {
  test.setTimeout(900_000)

  test('切换清理旧状态、迟到不覆盖、失败不伪装空态', async ({ browser }) => {
    const username = uniqueName('es-c23')
    const password = strongPassword('Init')
    const projectName = uniqueName('C23切换项目')
    const profileId = `c23-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)

    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const vA = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: 'A基线', reason: 'manual_save', expected_revision: view0.draft.revision,
    }, token)
    expect(vA.version.version_no).toBe(2)
    const preA = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    expect((preA.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const taskA = await submitOptimization(apiCtx.request, pid, token, `c23-a-${Date.now()}`)
    await waitCompleted(apiCtx.request, pid, taskA, token)

    const planning = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        objective: {
          sense: 'minimize',
          expression: 'grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om',
        },
        variables: {
          hp_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '60', upper_bound: '1000', unit: 'kW' },
        },
        constraints: {},
      },
      expected_revision: planning.revision,
    }, token)
    const view1 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const vB = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: 'B基线', reason: 'manual_save', expected_revision: view1.draft.revision,
    }, token)
    expect(vB.version.version_no).toBe(3)
    const preB = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    expect((preB.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const taskB = await submitOptimization(apiCtx.request, pid, token, `c23-b-${Date.now()}`)
    await waitCompleted(apiCtx.request, pid, taskB, token)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    // B 的结果响应在 C3 阶段延迟 6s、在 C2 阶段 500, A 始终直通。
    // 同一路由按阶段标志切换行为, 不得 unrouteAll 后重注册: 实证表明
    // unrouteAll({behavior:'wait'}) 阻塞等待在途延迟处理器后, 重注册的
    // 同 pattern 路由对后续请求(含直接 fetch)不再生效, 500 注入静默丢失。
    let resultPhase: 'delay' | 'fail' = 'delay'
    await page.route('**/api/projects/*/tasks/*/result', async (route) => {
      if (route.request().url().includes(`/tasks/${taskB}/result`)) {
        if (resultPhase === 'fail') {
          await route.fulfill({
            status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'c2-boom' }),
          })
          return
        }
        await new Promise((r) => setTimeout(r, 6000))
      }
      await route.continue()
    })
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    const evSelect = page.getByLabel('证据包状态')
    await expect(evSelect).toBeVisible({ timeout: 60_000 })

    // C3-1: 切到 A, 显示 A 的 50/v2。
    await evSelect.selectOption({ index: 0 })
    const aRow = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp_cap' })
    await expect(aRow).toBeVisible({ timeout: 30_000 })
    await expect(aRow.getByText(/50\.0000/)).toBeVisible()
    const kv = page.locator('.ies-kv')
    await expect(kv.getByText('v2', { exact: true })).toBeVisible()
    // C3-2: 等足 B 的 6s 迟到响应到达(已发出必达), 再断言 A 不被覆盖。
    await page.waitForTimeout(9000)
    await expect(kv.getByText('v3', { exact: true })).toHaveCount(0)
    await expect(kv.getByText('v2', { exact: true })).toBeVisible()
    await expect(aRow.getByText(/50\.0000/)).toBeVisible()
    await expect(page.locator('.ies-table__body .ies-table__row', { hasText: 'hp_cap' }).getByText(/60\.0000/)).toHaveCount(0)
    // C3-3: 切回 B, 旧预览/来源即刻清理(无 50 行、无 v2/v3、无确认框)。
    await evSelect.selectOption({ index: 1 })
    await expect(aRow).toHaveCount(0, { timeout: 10_000 })
    await expect(kv.getByText('v2', { exact: true })).toHaveCount(0)
    await expect(kv.getByText('v3', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('dialog')).toHaveCount(0)
    // B 到达后显示 60/v3。
    const bRow = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp_cap' })
    await expect(bRow.getByText(/60\.0000/)).toBeVisible({ timeout: 30_000 })
    await expect(kv.getByText('v3', { exact: true })).toBeVisible()

    // C2: B 的结果 GET 500 → 错误条可见, 两空态卡不渲染。
    // 同一路由切到 fail 相, 不得重注册(见上)。
    resultPhase = 'fail'
    await evSelect.selectOption({ index: 0 })
    await expect(bRow.getByText(/60\.0000/)).toHaveCount(0)
    await evSelect.selectOption({ index: 1 })
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('候选方案')).toHaveCount(0)
    await expect(page.getByText('规划解参数预览')).toHaveCount(0)
    await expect(bRow).toHaveCount(0)

    // 预期内噪声: C2 定向布置的 B 结果 500 各一次(精确核对, 其余零容忍)。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}/tasks/${taskB}/result`, status: 500, times: 1 },
    ])
    await context.close()
  })
})
