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

    // 真实用户导出: 捕获 excel POST 体核对同一证据包, 真实下载文件。
    // 导出从不隐式创建评估(缺评估只给显式引导, 见 R7); 收集/重试零 POST 断言止于此前。
    let excelBody: { evidence_package_id?: unknown } | null = null
    await context.route(
      (url) => url.pathname === `/api/projects/${pid}/exports/excel`,
      async (route) => {
        excelBody = route.request().postDataJSON() as { evidence_package_id?: unknown } | null
        await route.fallback()
      },
    )
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 180_000 }),
      excelBtn.click({ timeout: 30_000 }),
    ])
    expect(excelBody?.evidence_package_id).toBe(pkgId)
    const downloadPath = await download.path()
    expect(downloadPath).toBeTruthy()
    const { statSync } = await import('node:fs')
    expect(statSync(downloadPath as string).size).toBeGreaterThan(0)

    // 预期内噪声: 详情 500×1, /result 500×2(结果页重载 + 导出页)。
    session.assertNoErrors([
      { method: 'GET', path: detailPath, status: 500, times: 1 },
      { method: 'GET', path: resultPath, status: 500, times: 2 },
    ])
    await apiCtx.close()
    await context.close()
  })

  test('切换项目后旧条目不滞留、失败只剩错误重试', async ({ browser }) => {
    // 本场景验收真实项目间导航的失败/恢复，不证明同组件状态复用：
    // 经项目列表会卸载原 ResultsPage。收集启动清旧状态另由源码复审核对。
    // ?package= 只首读一次，不列为切换路径。
    const userName = uniqueName('es-r4sw-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 项目 A: 公开 API 脚手架 + 真实求解完成(本场景唯一求解)。
    const projectA = uniqueName('R4切换A')
    const pidA = await scaffoldProject(
      requestJson, apiCtx.request, userToken, projectA, uniqueName('r4sw-tariff'),
    )
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pidA}`, undefined, userToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pidA}/draft`, {
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
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pidA}/planning-config`, {
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
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pidA}/validation/run`, {}, userToken)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pidA}/config`, undefined, userToken)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pidA}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `r4sw-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pidA}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })
    const view = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pidA}/tasks/${taskId}/result`, undefined, userToken,
    )
    const pkgId: number = view.result.evidence.id
    expect(pkgId).toBeGreaterThan(0)
    const entryText = `任务 #${taskId} · 证据包 #${pkgId}`

    // 项目 B: 公开 API 仅创建空项目(无任务、无求解)。
    const projectB = uniqueName('R4切换B')
    const createdB = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectB,
      baseline_resolution: '1h',
      baseline_start_year: 2025,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pidB: number = createdB.project?.id ?? createdB.id
    expect(pidB).toBeTruthy()
    const bListPath = `/api/projects/${pidB}/tasks`

    const context = await browser.newContext()
    const page = await context.newPage()
    // B 的任务列表按标志失败(只在点结果分析前打开, 不污染 B 任务页加载)。
    // 仅命中结果页任务上下文查询(status=completed&limit=100, 与当前
    // useEffectiveTaskContext 语义一致); 任务中心页自身的 limit=100 轮询等
    // 其他查询一律走真实后端, 不在此 counted。
    let failBList = false
    await context.route(
      (url) =>
        url.pathname === bListPath &&
        url.searchParams.get('status') === 'completed' &&
        url.searchParams.get('limit') === '100',
      (route) => failBList
        ? route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: '{"detail":"r4 injected switch failure"}',
        })
        : route.fallback(),
    )
    const session = await createSession(page, userName, password)

    // A 结果页成功, 旧条目可见。
    await page.goto(`/projects/${pidA}/results`)
    await expect(page.getByText(entryText)).toBeVisible({ timeout: 60_000 })

    // 客户端路由切到 B：先到列表，再进 B，再点结果分析。
    await page.getByText('项目列表').first().click({ timeout: 30_000 })
    await page.getByText(projectB).click({ timeout: 30_000 })
    // 项目页 index 重定向到任务中心子路由。
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks/)
    // 先登记定向失败响应观察(任务上下文查询 500), 点击后再等待其真实到达,
    // 证明失败态与该注入对应后才断言 UI。
    const targetedFailure = page.waitForResponse(
      (res) =>
        res.request().method() === 'GET' &&
        new URL(res.url()).pathname === bListPath &&
        new URL(res.url()).searchParams.get('status') === 'completed' &&
        res.status() === 500,
      { timeout: 30_000 },
    )
    failBList = true
    await page.getByText('结果分析').first().click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await targetedFailure

    // 失败态只剩错误+重试: A 旧条目不再显示, 无伪空态, 无旧包导出按钮。
    await expect(page.getByRole('button', { name: '重试', exact: true }).first()).toBeVisible({ timeout: 60_000 })
    expect(await page.getByText(entryText).count()).toBe(0)
    expect(await page.getByText('该项目暂无结果').count()).toBe(0)
    expect(await page.getByRole('button', { name: /导出 Excel/i }).count()).toBe(0)

    // 解除失败后重试: B 真实空态, A 条目仍不出现。
    failBList = false
    await page.getByRole('button', { name: '重试', exact: true }).first().click({ timeout: 30_000 })
    await expect(page.getByText('该项目暂无结果')).toBeVisible({ timeout: 60_000 })
    expect(await page.getByText(entryText).count()).toBe(0)

    session.assertNoErrors([
      { method: 'GET', path: bListPath, status: 500, times: 1 },
      // 空项目 B 任务页读尚无保存的计算配置 404(R4 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pidB}/config`, status: 404, times: 1 },
    ])
    await apiCtx.close()
    await context.close()
  })
})
