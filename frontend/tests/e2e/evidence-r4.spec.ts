// R4 任务详情/证据收集失败可见: 详情或 /result 补充读取失败不得伪装
// “暂无结果”或降到其他包；失败明确可重试，重试仅重读。
//
// 成功路径真实不 mock: 求解脚手架走公开 API(与 hourly-r3 同源)，同一可用
// 结果复用于三个失败场景；failure 仅用定向 HTTP 500 拦截详情或 /result；
// 不注库、不注入页面状态、不 mock 成功数据。

import { test, expect } from '@playwright/test'
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

test.describe('R4: 证据收集失败可见', () => {
  test.setTimeout(900_000)

  test('详情与result失败不伪装空态、重试恢复同一证据', async ({ browser }) => {
    const userName = uniqueName('es-r4-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 公开 API 准备: 项目 + 双泵规划 + 真实求解完成(与 hourly-r3 同源)。
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, userToken, uniqueName('R4项目'), uniqueName('r4-tariff'),
    )
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, userToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'st-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'st-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'st-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
            hp2: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    }, userToken)
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
    }, userToken)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, userToken)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, userToken)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `r4-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // 同一可用结果的真实身份(失败前后对照用，不替代被测读取)。
    const view = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, userToken,
    )
    const pkgId: number = view.result.evidence.id
    expect(pkgId).toBeGreaterThan(0)
    const entryText = `任务 #${taskId} · 证据包 #${pkgId}`

    const context = await browser.newContext()
    const page = await context.newPage()
    // 记录全程写请求: 收集/重试只许读, 不许新任务/评估 POST。
    const writePosts: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/(tasks|assessments)/.test(req.url())) {
        writePosts.push(`${req.method()} ${req.url()}`)
      }
    })
    const detailPath = `/api/projects/${pid}/tasks/${taskId}`
    const resultPath = `/api/projects/${pid}/tasks/${taskId}/result`
    // 单一拦截器按标志失败/放行(避免 unroute 闭包引用比对删不掉路由):
    // 失败只注入真实 500, 放行走真实响应。
    let failDetail = false
    let failResult = false
    await context.route(
      (url) => url.pathname === detailPath || url.pathname === resultPath,
      (route) => {
        const pathname = new URL(route.request().url()).pathname
        if ((pathname === detailPath && failDetail) || (pathname === resultPath && failResult)) {
          return route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: '{"detail":"r4 injected collect failure"}',
          })
        }
        return route.fallback()
      },
    )
    const session = await createSession(page, userName, password)

    // 阶段一: 结果页详情 500 → 全局失败可见、可重试, 不伪装空条目。
    failDetail = true
    await page.goto(`/projects/${pid}/results`)
    await expect(page.getByRole('button', { name: '重试', exact: true }).first()).toBeVisible({ timeout: 60_000 })
    expect(await page.getByText(entryText).count()).toBe(0)
    // 失败期不得伪装空态。
    expect(await page.getByText('该项目暂无结果').count()).toBe(0)
    expect(writePosts).toEqual([])
    failDetail = false
    await page.getByRole('button', { name: '重试', exact: true }).first().click({ timeout: 30_000 })
    await expect(page.getByText(entryText)).toBeVisible({ timeout: 60_000 })
    expect(writePosts).toEqual([])

    // 阶段二: 结果页 /result 500(详情成功) → 同样整组失败, 非“暂无结果”。
    failResult = true
    await page.reload()
    await expect(page.getByRole('button', { name: '重试', exact: true }).first()).toBeVisible({ timeout: 60_000 })
    expect(await page.getByText(entryText).count()).toBe(0)
    expect(await page.getByText('该项目暂无结果').count()).toBe(0)
    expect(writePosts).toEqual([])
    failResult = false
    await page.getByRole('button', { name: '重试', exact: true }).first().click({ timeout: 30_000 })
    await expect(page.getByText(entryText)).toBeVisible({ timeout: 60_000 })
    expect(writePosts).toEqual([])

    // 阶段三: 独立导出页 /result 500 → 明确加载失败、可重试;
    // 不显示“暂无可用结果”, 导出按钮禁用(不允许错包导出)。
    failResult = true
    await page.goto(`/projects/${pid}/export`)
    const retryBtn = page.getByRole('button', { name: '重试', exact: true })
    await expect(retryBtn).toBeVisible({ timeout: 60_000 })
    expect(await page.getByText(/暂无可用结果/).count()).toBe(0)
    const excelBtn = page.getByRole('button', { name: /导出 Excel/i })
    await expect(excelBtn).toBeDisabled()
    expect(writePosts).toEqual([])
    failResult = false
    await retryBtn.click({ timeout: 30_000 })
    await expect(excelBtn).toBeEnabled({ timeout: 60_000 })
    expect(await page.getByText(/暂无可用结果/).count()).toBe(0)
    expect(writePosts).toEqual([])

    // 预期内噪声: 详情 500×1, /result 500×2(结果页重载 + 导出页)。
    session.assertNoErrors([
      { method: 'GET', path: detailPath, status: 500, times: 1 },
      { method: 'GET', path: resultPath, status: 500, times: 2 },
    ])
    await apiCtx.close()
    await context.close()
  })
})
